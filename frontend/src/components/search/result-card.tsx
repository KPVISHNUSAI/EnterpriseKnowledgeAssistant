"use client";

import Link from "next/link";
import { useId, useState } from "react";
import { ChevronDown, FileText } from "lucide-react";
import { Badge, Card } from "@/components/ui/feedback";
import type { SearchResult } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { fileTypeLabel, formatScore } from "@/lib/format";
import { HighlightedText } from "./highlighted-text";

function describeMatch(result: SearchResult): string {
  const { explanation: e } = result;
  const methods = [
    e.vector_rank !== null && e.vector_rank !== undefined ? `semantic search (rank ${e.vector_rank})` : null,
    e.keyword_rank !== null && e.keyword_rank !== undefined ? `keyword search (rank ${e.keyword_rank})` : null,
  ].filter(Boolean);
  const found = methods.length ? `Found by ${methods.join(" and ")}.` : "Matched by the search index.";
  const fused = methods.length > 1 ? " The two rankings were fused with reciprocal rank fusion (RRF)." : "";
  const reranked =
    e.rerank_score !== null && e.rerank_score !== undefined ? " A cross-encoder reranked the candidates." : "";
  return `${found}${fused}${reranked}`;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 border-b border-slate-100 py-1.5 last:border-0">
      <dt className="text-slate-500">{label}</dt>
      <dd className="text-right font-medium text-slate-800">{children}</dd>
    </div>
  );
}

function WhyMatched({ result }: { result: SearchResult }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const e = result.explanation;
  const terms = e.matched_terms ?? [];
  return (
    <div className="mt-3">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 text-xs font-medium text-accent-700 hover:underline"
        data-testid="why-matched-toggle"
      >
        <ChevronDown aria-hidden className={cn("size-3.5 transition-transform", open && "rotate-180")} />
        Why this matched
      </button>
      {open ? (
        <div id={panelId} className="mt-2 rounded-lg bg-slate-50 p-3 text-xs" data-testid="why-matched">
          <p className="mb-2 text-slate-700">{describeMatch(result)}</p>
          <dl>
            <Row label="Final rank">#{e.final_rank}</Row>
            <Row label="Vector (semantic)">
              {e.vector_rank !== null && e.vector_rank !== undefined
                ? `rank ${e.vector_rank}, similarity ${formatScore(e.vector_similarity)}`
                : "not matched"}
            </Row>
            <Row label="Keyword (full text)">
              {e.keyword_rank !== null && e.keyword_rank !== undefined
                ? `rank ${e.keyword_rank}, score ${formatScore(e.keyword_score)}`
                : "not matched"}
            </Row>
            <Row label="RRF fusion score">{formatScore(e.rrf_score, 4)}</Row>
            <Row label="Rerank score">{formatScore(e.rerank_score)}</Row>
            <Row label="Matched terms">
              {terms.length ? (
                <span className="inline-flex flex-wrap justify-end gap-1">
                  {terms.map((term) => (
                    <Badge key={term} tone="accent">
                      {term}
                    </Badge>
                  ))}
                </span>
              ) : (
                "none (semantic match)"
              )}
            </Row>
          </dl>
        </div>
      ) : null}
    </div>
  );
}

export function ResultCard({ result }: { result: SearchResult }) {
  const [expanded, setExpanded] = useState(false);
  const params = new URLSearchParams();
  if (result.page_number) params.set("page", String(result.page_number));
  params.set("chunk", result.chunk_id);
  const href = `/documents/${result.document_id}?${params}`;
  const where = [
    result.page_number ? `Page ${result.page_number}` : null,
    result.heading_path || result.section_title,
  ].filter(Boolean);

  return (
    <Card className="p-4" data-testid="search-result">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <Link
            href={href}
            className="inline-flex items-center gap-2 text-base font-medium text-slate-900 hover:text-accent-700 hover:underline"
          >
            <FileText aria-hidden className="size-4 shrink-0 text-slate-400" />
            <span className="break-words">{result.document_title}</span>
          </Link>
          <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-500">
            {result.file_type ? <Badge>{fileTypeLabel(result.file_type)}</Badge> : null}
            <span>{result.workspace_name}</span>
            {where.length ? <span>· {where.join(" · ")}</span> : null}
          </p>
        </div>
        <span className="text-xs text-slate-500" title="Final ranking score">
          Score {formatScore(result.score)}
        </span>
      </div>
      <p
        className={cn("mt-3 text-sm leading-relaxed whitespace-pre-wrap text-slate-700", !expanded && "line-clamp-6")}
        data-testid="search-snippet"
      >
        <HighlightedText segments={result.highlights} fallback={result.content} />
      </p>
      {result.content.length > 400 ? (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="mt-1 text-xs font-medium text-accent-700 hover:underline"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      ) : null}
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <WhyMatched result={result} />
        <Link href={href} className="text-sm font-medium text-accent-700 hover:underline">
          {result.page_number ? `Open at page ${result.page_number}` : "Open document"}
        </Link>
      </div>
    </Card>
  );
}
