"""Command line for the evaluation suite: `cd backend && uv run python -m eval --help`.

run     reset the evaluation database, load and ingest the corpus, evaluate, and write
        eval/reports/<name>/ (JSON per part plus report.md)
report  regenerate eval/reports/<name>/report.md from the JSON files there
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from eval.dataset import DEFAULT_VERSION
from eval.report import REPORTS, write_report

CACHE = Path(__file__).parent / ".cache"
DEFAULT_LLM_MODEL = "llama3.1:8b"
DEFAULT_LLM_BASE_URL = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible API
LLM_CONFIGS = ("vector", "keyword", "hybrid", "hybrid_rerank")
_EVAL_DATABASE = re.compile(r"eka_eval[a-z0-9_]*")
# Room for the prompt in a 4096-token window: system prompt, sources, question and the
# LLM_MAX_OUTPUT_TOKENS answer (see docs/EVALUATION.md).
CONTEXT_MAX_TOKENS = 2200
LLM_MAX_OUTPUT_TOKENS = 800


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "report":
        print(write_report(REPORTS / args.name))
        return 0
    llm_configs = [config for config in args.llm_configs.split(",") if config]
    unknown = sorted(set(llm_configs) - set(LLM_CONFIGS))
    if unknown:
        raise SystemExit(f"unknown --llm-configs {unknown}; choose from {', '.join(LLM_CONFIGS)}")
    admin_url = _pin_environment(args.llm_model, args.llm_base_url)

    from eval import runner  # only now: app settings are read on first use and cached
    from eval.dataset import load

    cases = load(args.dataset)
    if args.cases:
        wanted = args.cases.split(",")
        unknown = sorted(set(wanted) - {case.id for case in cases})
        if unknown:
            raise SystemExit(f"unknown case ids {unknown}")
        cases = [case for case in cases if case.id in wanted]
    if args.limit:
        cases = cases[: args.limit]
    path = runner.run(
        name=args.name,
        dataset_version=args.dataset,
        cases=cases,
        admin_url=admin_url,
        retrieval=not args.no_retrieval,
        extractive=not args.no_extractive,
        llm_configs=llm_configs,
        command_line=shlex.join(["uv", "run", "python", "-m", "eval", *(argv or sys.argv[1:])]),
        include_flagged=args.include_flagged,
    )
    print(path)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval",
        description="Evaluate retrieval and grounded answering on the synthetic corpus.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run",
        help="reset the evaluation database, ingest the corpus and evaluate",
        description=(
            "Resets the database EKA_EVAL_DB (default eka_eval on localhost:EKA_POSTGRES_PORT, "
            "default 5436; override the URLs with EVAL_DATABASE_URL, "
            "EVAL_MIGRATION_DATABASE_URL and EVAL_ADMIN_DATABASE_URL), uses Redis "
            "EVAL_REDIS_URL (default redis://localhost:6381/11), loads the corpus with the "
            "local embedding and reranker models (offline), then evaluates retrieval, the "
            "extractive generator and optionally an LLM behind an OpenAI-compatible API."
        ),
    )
    run.add_argument("--name", default=date.today().isoformat(), help="results directory name")
    run.add_argument("--dataset", default=DEFAULT_VERSION, help="dataset version in eval/datasets/")
    run.add_argument("--no-retrieval", action="store_true", help="skip the retrieval evaluation")
    run.add_argument("--no-extractive", action="store_true", help="skip the extractive generator")
    run.add_argument(
        "--llm-configs",
        default="",
        help=f"retrieval configurations to answer with the LLM, comma-separated: "
        f"{', '.join(LLM_CONFIGS)} (default: none)",
    )
    run.add_argument(
        "--llm-model",
        default=os.environ.get("EVAL_LLM_MODEL", DEFAULT_LLM_MODEL),
        help=f"model name on the LLM server (default {DEFAULT_LLM_MODEL})",
    )
    run.add_argument(
        "--llm-base-url",
        default=os.environ.get("EVAL_LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
        help=f"OpenAI-compatible base URL (default {DEFAULT_LLM_BASE_URL})",
    )
    run.add_argument("--cases", default="", help="only these case ids, comma-separated")
    run.add_argument(
        "--include-flagged",
        action="store_true",
        help="let text addressed to AI assistants into answers (the quarantine off), to "
        "measure what removing it changes",
    )
    run.add_argument("--limit", type=int, default=0, help="only the first N cases")
    report = commands.add_parser("report", help="regenerate report.md from the JSON results")
    report.add_argument("--name", required=True, help="results directory name")
    return parser


def _pin_environment(llm_model: str, llm_base_url: str) -> str:
    """Set every setting that shapes the results before `app` is imported, so neither
    .env nor the shell changes what is measured. Returns the admin database URL."""
    port = os.environ.get("EKA_POSTGRES_PORT", "5436")
    database = os.environ.get("EKA_EVAL_DB", "eka_eval")
    app_url = os.environ.get(
        "EVAL_DATABASE_URL",
        f"postgresql+psycopg://eka_app:eka_app_dev_password@localhost:{port}/{database}",
    )
    owner_url = os.environ.get(
        "EVAL_MIGRATION_DATABASE_URL",
        f"postgresql+psycopg://eka_owner:eka_owner_dev_password@localhost:{port}/{database}",
    )
    admin_url = os.environ.get(
        "EVAL_ADMIN_DATABASE_URL",
        f"postgresql+psycopg://postgres:postgres_dev_password@localhost:{port}/postgres",
    )
    redis_url = os.environ.get("EVAL_REDIS_URL", "redis://localhost:6381/11")
    for url in (app_url, owner_url):
        if not _EVAL_DATABASE.fullmatch(urlsplit(url).path.lstrip("/")):
            raise SystemExit("the evaluation database name must start with eka_eval (it is reset)")
    if (urlsplit(redis_url).path.strip("/") or "0") == "0":
        raise SystemExit("EVAL_REDIS_URL must not use Redis database 0")
    os.environ.update(
        {
            "APP_ENV": "development",
            "SECRET_KEY": secrets.token_urlsafe(48),
            "LOG_LEVEL": "WARNING",
            "LOG_FORMAT": "console",
            "DATABASE_URL": app_url,
            "MIGRATION_DATABASE_URL": owner_url,
            "DB_APP_ROLE": urlsplit(app_url).username or "eka_app",
            "REDIS_URL": redis_url,
            "CELERY_BROKER_URL": redis_url,
            "STORAGE_BACKEND": "local",
            "LOCAL_STORAGE_PATH": str(CACHE / "storage"),
            "RATE_LIMIT_ENABLED": "false",
            "METRICS_ENABLED": "false",
            "EMBEDDING_PROVIDER": "sentence_transformers",
            "EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
            "EMBEDDING_DIMENSIONS": "384",
            "EMBEDDING_DEVICE": "auto",
            "EMBEDDING_QUERY_PREFIX": "Represent this sentence for searching relevant passages: ",
            "EMBEDDING_DOCUMENT_PREFIX": "",
            "EMBEDDING_CACHE_TTL_SECONDS": "0",  # every run embeds from scratch
            "RERANKER_PROVIDER": "cross_encoder",
            "RERANKER_MODEL": "cross-encoder/ms-marco-MiniLM-L6-v2",
            "RERANKER_DEVICE": "auto",
            "RERANK_ENABLED": "true",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "LLM_PROVIDER": "openai_compatible",
            "LLM_BASE_URL": llm_base_url,
            "LLM_MODEL": llm_model,
            "LLM_MAX_OUTPUT_TOKENS": str(LLM_MAX_OUTPUT_TOKENS),
            "LLM_TEMPERATURE": "0",
            "LLM_TIMEOUT_SECONDS": "300",
            "LLM_MAX_RETRIES": "1",
            "LLM_INPUT_COST_PER_MTOK": "0",
            "LLM_OUTPUT_COST_PER_MTOK": "0",
            "RETRIEVAL_MODE": "hybrid",
            "RETRIEVAL_VECTOR_TOP_K": "40",
            "RETRIEVAL_KEYWORD_TOP_K": "40",
            "RETRIEVAL_RRF_K": "60",
            "RETRIEVAL_FUSED_TOP_K": "30",
            "RETRIEVAL_FINAL_TOP_K": "8",
            "CONTEXT_MAX_TOKENS": str(CONTEXT_MAX_TOKENS),
            "HNSW_EF_SEARCH": "100",
            "FTS_CONFIG": "english",
            "QUERY_REWRITE_ENABLED": "true",
            "CHAT_HISTORY_MESSAGES": "6",
            "MAX_QUERY_CHARS": "2000",
            "CHUNK_TARGET_TOKENS": "350",
            "CHUNK_MAX_TOKENS": "500",
            "CHUNK_OVERLAP_TOKENS": "50",
        }
    )
    for name in ("RETRIEVAL_MIN_RERANK_SCORE", "LLM_QUERY_REWRITE_MODEL", "LLM_API_KEY"):
        os.environ.pop(name, None)
    return admin_url


if __name__ == "__main__":
    sys.exit(main())
