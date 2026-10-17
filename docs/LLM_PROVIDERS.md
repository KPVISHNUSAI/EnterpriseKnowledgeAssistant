# LLM providers

The answer pipeline (retrieval, permission filtering, citation validation, abstention) is the
same for every provider; only the text-generation step differs. The code lives in
`backend/app/providers/llm/`: `base.py` is the interface, `anthropic.py` and
`openai_compatible.py` are the implementations, and `build_llm(settings)` picks one.

| `LLM_PROVIDER` | Backend | You need | Notes |
|---|---|---|---|
| `extractive` (default) | none | nothing | `build_llm()` returns `None`; the answer is composed from the retrieved passages, so the whole product runs offline with no key. |
| `anthropic` + `ANTHROPIC_PLATFORM=anthropic` | Claude API | `ANTHROPIC_API_KEY` | Default model `claude-opus-5-5`. |
| `anthropic` + `ANTHROPIC_PLATFORM=bedrock` | Claude in Amazon Bedrock | `AWS_REGION` and AWS credentials | Model ids carry an `anthropic.` prefix. |
| `openai_compatible` | Ollama, vLLM, LM Studio, any `/chat/completions` server | `LLM_BASE_URL` | Optional `LLM_API_KEY` bearer token. |

## Contract for callers

* `provider.stream(request)` is a generator: nothing happens, and no error is raised, until the
  first `next()`. Wrap the iteration, not just the call, in `except LLMError`.
* It yields `LLMTextDelta` items and then exactly one `LLMStreamEnd` whose `response` holds the
  full text, usage, `stop_reason`, the model that answered and the provider's request id.
  `generate()` returns that same `LLMResponse`.
* `stop_reason` is `end_turn`, `max_tokens` or `refusal`; any other value the provider reports
  is passed through (for example Claude's `stop_sequence` or `model_context_window_exceeded`).
  A refusal or a truncated answer is a normal response, not an error.
* Close the generator (`generator.close()`) from the thread that iterates it: that releases the
  HTTP response. Cancellation is only noticed between events, and no text arrives while Claude
  is thinking, so a disconnect during thinking is acted on when the first text shows up.
* Provider objects are thread-safe and meant to be shared (`get_llm()` caches one per process).

## Settings

Everything is read from environment variables (or `.env`); nothing is hardcoded.

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `LLM_PROVIDER` | `extractive` | all | `anthropic`, `openai_compatible` or `extractive`. |
| `LLM_MODEL` | `claude-opus-5-5` | both providers | Default model. For Bedrock use `anthropic.claude-opus-5-5`; for Ollama e.g. `llama3.1:8b`. |
| `LLM_QUERY_REWRITE_MODEL` | `LLM_MODEL` | both | Optional cheaper model for follow-up question rewriting (`settings.query_rewrite_model`). |
| `LLM_MAX_OUTPUT_TOKENS` | `16000` | both | Default output cap; a request can override it. |
| `LLM_TIMEOUT_SECONDS` | `120` | both | Read/write timeout per request. Connecting is capped at 5 s so an unreachable server fails fast. |
| `LLM_MAX_RETRIES` | `2` | both | Retries after the first attempt, before any text has been delivered. |
| `LLM_TEMPERATURE` | `0.1` | `openai_compatible` only | Claude models reject sampling parameters, so Anthropic never receives one. |
| `ANTHROPIC_API_KEY` | none | Claude API | Required on the Claude API. Never read for Bedrock. |
| `ANTHROPIC_PLATFORM` | `anthropic` | anthropic | `anthropic` or `bedrock`. |
| `ANTHROPIC_EFFORT` | `medium` | anthropic | `low`, `medium`, `high`, `xhigh` or `max`: the thinking depth / token spend of answers. |
| `ANTHROPIC_REWRITE_EFFORT` | `low` | anthropic | Effort the answer pipeline should pass for rewrite requests (see below). |
| `ANTHROPIC_FALLBACKS` | `default` | Claude API only | `default` enables server-side refusal fallbacks, `off` disables them. |
| `AWS_REGION` | none | Bedrock | Region of the Bedrock endpoint. The AWS SDK variable `AWS_DEFAULT_REGION` also works. |
| `LLM_BASE_URL` | none | `openai_compatible` | The server's API root, e.g. the `/v1` URL. `/chat/completions` is appended. |
| `LLM_API_KEY` | none | `openai_compatible` | Sent as `Authorization: Bearer ...` when set. |
| `LLM_*_COST_PER_MTOK` | `0` | answer pipeline | Prices used for the estimated-cost metric. |

The Anthropic SDK also reads `ANTHROPIC_BASE_URL` (route Claude API calls through a gateway),
and the Bedrock client reads the standard AWS variables described below.

## Claude API (`anthropic`)

```
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-opus-5-5
LLM_QUERY_REWRITE_MODEL=claude-haiku-5-5   # optional
ANTHROPIC_EFFORT=medium
```

What the provider sends, and why:

* **Official SDK**, client `timeout` = `LLM_TIMEOUT_SECONDS`, `max_retries` = `LLM_MAX_RETRIES`.
  The SDK retries connection errors, 408/409/429 and 5xx before the response starts, honouring
  `Retry-After`; nothing is replayed once text has been streamed. The SDK would sleep through
  any `Retry-After`, however long, so a small middleware caps it at 8 s: a longer value fails
  the call at once with `LLMRateLimitError` carrying `retry_after`, instead of pinning a worker
  thread.
* **Streaming always**, also for `generate()`: long outputs would otherwise hit HTTP timeouts.
  Only `text` blocks are answer text; thinking blocks are ignored.
* **`thinking: {"type": "adaptive"}` and `output_config: {"effort": ...}`** (the request's
  `effort`, else `ANTHROPIC_EFFORT`). **No `temperature`, `top_p` or `top_k`**: current
  models reject them. Use current-generation models (Claude 4.6 and later). Older ones such as
  Haiku 4.5 or Sonnet 4.5 do not accept `effort` or adaptive thinking; the API answers HTTP
  400, which surfaces as a generic `LLMError`.
* Thinking tokens count against the output cap. Give rewrite requests headroom
  (`max_output_tokens` of 1024 or more); a tiny cap can be spent entirely on thinking and
  yield an empty answer with `stop_reason == "max_tokens"`.
* **Per-request overrides** on `LLMRequest`: `model`, `max_output_tokens`, `effort`.
  `temperature` is ignored. For a follow-up rewrite the answer pipeline should pass
  `model=settings.query_rewrite_model`, `effort=settings.anthropic_rewrite_effort`,
  `allow_fallbacks=False` and `purpose="query_rewrite"`.
* **Refusals and truncation** are not errors. They arrive as
  `LLMResponse.stop_reason == "refusal"` and `"max_tokens"` (with whatever text was produced).
* **Server-side refusal fallbacks** (`betas=["server-side-fallback-2026-07-01"]`,
  `fallbacks="default"`, via `client.beta.messages.stream`) are used only when
  `ANTHROPIC_PLATFORM=anthropic`, `ANTHROPIC_FALLBACKS=default`, the request allows them
  (`allow_fallbacks`) and the model is not a Haiku model (Haiku has no server-side fallback).
  Otherwise the plain `client.messages.stream` is used. After a fallback,
  `LLMResponse.model` names the model that actually answered, and `usage` covers only the
  attempt that produced the answer.

## Amazon Bedrock (`anthropic` + `ANTHROPIC_PLATFORM=bedrock`)

```
LLM_PROVIDER=anthropic
ANTHROPIC_PLATFORM=bedrock
AWS_REGION=us-east-1
LLM_MODEL=anthropic.claude-opus-5-5
LLM_QUERY_REWRITE_MODEL=anthropic.claude-haiku-5-5   # optional
```

* Uses the SDK's `AnthropicBedrockMantle` client (the Messages-API Bedrock endpoint,
  `https://bedrock-mantle.<region>.api.aws/anthropic`). The legacy `InvokeModel` integration
  is not used.
* **Credentials come from the standard AWS chain, never from application settings**:
  environment variables, shared config or SSO profile, ECS task role, EC2/EKS role. A
  short-lived Bedrock bearer token in `AWS_BEARER_TOKEN_BEDROCK` is also honoured by the SDK.
  Requests are signed lazily, so missing or unusable credentials show up on the first request
  as an `LLMConfigurationError` (nothing is sent), not at startup.
  Per Anthropic's Bedrock guide the role needs `bedrock-mantle:CreateInference` on the allowed
  model ARNs, and model access must be enabled in the Bedrock console; confirm against the
  current AWS documentation when writing the IAM policy.
* **Model ids need the `anthropic.` prefix.** Switching `ANTHROPIC_PLATFORM` while leaving the
  default `LLM_MODEL=claude-opus-5-5` would send an id Bedrock does not accept, so a bare
  `claude-...` id in `LLM_MODEL` or `LLM_QUERY_REWRITE_MODEL` is rejected at construction with a
  configuration error that names the fix. Any other id is passed through to Bedrock unchanged.
* No server-side fallbacks (`ANTHROPIC_FALLBACKS` is ignored), so a refusal stays a refusal.
* Everything else (streaming, adaptive thinking, effort, error mapping) is shared with the
  Claude API path. A missing region is a `LLMConfigurationError` at construction.

## OpenAI-compatible servers (`openai_compatible`)

```
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://localhost:11434/v1     # Ollama
LLM_MODEL=llama3.1:8b
# LLM_API_KEY=                            # only if the server requires a bearer token
```

| Server | `LLM_BASE_URL` (default port) |
|---|---|
| Ollama | `http://localhost:11434/v1` |
| vLLM | `http://<host>:8000/v1` |
| LM Studio | `http://localhost:1234/v1` |

From a container, a server on the host is reachable as `host.docker.internal` (Docker Desktop).

* `POST {LLM_BASE_URL}/chat/completions` with `model`, `messages` (system message first),
  `max_tokens`, `temperature` (the request's, else `LLM_TEMPERATURE`; `0.0` is respected and
  `None` omits it), `stream: true` and `stream_options: {"include_usage": true}`.
  `generate()` is a plain non-streaming request (used for query rewriting).
* Streaming parses server-sent events (`data:` lines) until `[DONE]`. Only
  `delta.content` is answer text, so `reasoning_content` from reasoning models is ignored
  (models that inline `<think>` tags in `content` are not stripped). A stream that ends with
  neither `[DONE]` nor a `finish_reason`, an `error` chunk, or malformed JSON is an
  `LLMUnavailableError`, never a silently short answer.
* **Retries**: 429, 5xx, timeouts and connection failures are retried up to `LLM_MAX_RETRIES`
  times with exponential backoff (0.5 s, 1 s, 2 s, ... capped at 8 s), but only before the
  response starts. `Retry-After` is honoured when it is longer than the backoff; if it is
  longer than 8 s the `LLMRateLimitError` (with `retry_after`) is raised immediately instead of
  holding a worker thread.
* **Usage** comes from the server's usage block. If the server omits it, usage is estimated
  as characters / 4 and an `llm.usage_estimated` log record with `usage_estimated=true` is
  written; cost figures are then approximate.
* `finish_reason` is mapped to the shared vocabulary: `stop` to `end_turn`, `length` to
  `max_tokens`, `content_filter` to `refusal`.
* Redirects are not followed (they would carry the bearer token elsewhere): a 3xx is reported
  as a configuration error pointing at `LLM_BASE_URL`.
* `LLMResponse.model` is the requested model, and `request_id` the server's `x-request-id`
  when it sends one.

Local-model gotchas:

* Ollama picks a default context window from the available VRAM (4096 tokens on the machine
  used for testing; see `context_length` in `GET /api/ps`). That is smaller than the default
  `CONTEXT_MAX_TOKENS=6000` of retrieved passages, so the server silently truncates the
  prompt. Raise the server's context (`OLLAMA_CONTEXT_LENGTH`, for example `8192`) or lower
  `CONTEXT_MAX_TOKENS`.
* The first request after the model was unloaded includes the load time; `LLM_TIMEOUT_SECONDS`
  covers it.
* Citation discipline and abstention quality depend on the model. The pipeline validates
  citations against the supplied passages regardless.

## Errors, retries and metrics

Both providers map failures onto `app.providers.llm.base`:

| Situation | Class (`kind`) | `retryable` |
|---|---|---|
| Missing API key, base URL or region; a Claude API model id on Bedrock; missing or unusable AWS credentials; 401/403; 404 (unknown model or endpoint); 3xx (OpenAI-compatible: redirects are not followed) | `LLMConfigurationError` (`configuration`) | no |
| 429 | `LLMRateLimitError` (`rate_limited`), `retry_after` from `Retry-After` when sent | yes |
| Timeout | `LLMTimeoutError` (`timeout`) | yes |
| 5xx and 529 overloaded, connection failure, dropped, truncated or malformed stream | `LLMUnavailableError` (`unavailable`) | yes |
| Any other 4xx (400, 402, 413, 422, ...) | `LLMError` (`error`) | no |

* Failures that happen after text was already streamed (mid-stream network errors, an `error`
  event inside the stream) use the same classes; the caller keeps the deltas it received.
* `eka_llm_errors_total{provider,kind}` counts each failed call once: the retries inside one
  call are not counted separately. Construction-time configuration errors are counted too.
* Error messages are written by us and never contain provider-supplied text, prompts, answers
  or keys. Each failure logs one `llm.error` record with `provider`, `kind`, `detail`, `model`,
  `purpose` and, for Anthropic, `error_class`, `status`, `error_type` and
  `provider_request_id` (quote that id to Anthropic support). Exceptions that are not provider
  failures (bugs) are not mapped and propagate unchanged.
* The Anthropic SDK logs whole request bodies (prompts, retrieved document text) at DEBUG. The
  provider module therefore pins the `anthropic` and `httpx2` loggers to WARNING, whatever
  `LOG_LEVEL` or `ANTHROPIC_LOG` say. `app/core/logging.py` already does the same for `httpx`.

## What was and was not tested

Automated (no network, no API key): `backend/tests/test_llm_providers_anthropic.py` runs the
real `anthropic` SDK against a fake HTTP transport that replays Messages-API server-sent
events and error responses; `backend/tests/test_llm_providers_openai_compatible.py` does the
same for the httpx provider. Together they cover streaming deltas and usage, refusal and
`max_tokens`, every error mapping including `Retry-After`, in-stream errors, mid-stream
network failures, closing a stream early (the HTTP response is released), the fallback beta
being sent or withheld per setting, request override and model, Bedrock client selection and
region wiring (construction only), configuration errors, SSE parsing with the usage chunk and
`[DONE]`, retries and backoff, the usage estimate, a 200 response that is not a message stream
(a proxy's page), the real SDK's behaviour with an empty AWS credential chain, and that no
prompt, answer or key reaches the logs or error messages.

Live:

* **Ollama (`llama3.1:8b`) through `OpenAICompatibleProvider`: tested.** The opt-in tests
  `pytest -m slow tests/test_llm_providers_openai_compatible.py` stream a short completion
  and run a non-streaming `generate()` against `http://localhost:11434/v1`; they skip unless a
  server answers (override with `EKA_TEST_OLLAMA_BASE_URL` / `EKA_TEST_OLLAMA_MODEL`).
* **Claude API: not tested live.** No API key was available. Request shapes follow Anthropic's
  current SDK documentation and are asserted against the fake transport only, so model-side
  behaviour (adaptive thinking, `effort`, the fallback beta) has not been confirmed against the
  real service.
* **Amazon Bedrock: not tested live.** There were no AWS credentials. Only client selection
  and endpoint wiring are covered, with request signing disabled in the test.
* **vLLM and LM Studio: not tested.** They are expected to work because they implement the
  same `/chat/completions` protocol, but only Ollama was exercised.
