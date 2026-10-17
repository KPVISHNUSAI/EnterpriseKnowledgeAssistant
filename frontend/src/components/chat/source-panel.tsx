"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import { EyeOff, X } from "lucide-react";
import { IconButton } from "@/components/ui/button";
import { Badge } from "@/components/ui/feedback";
import type { SourceRef } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { fileTypeLabel, formatScore } from "@/lib/format";

function scores(source: SourceRef): Array<[string, string]> {
  const r = source.relevance;
  const all: Array<[string, number | null | undefined]> = [
    ["Rerank", r.rerank_score],
    ["Similarity", r.vector_similarity],
    ["Keyword", r.keyword_score],
    ["RRF", r.rrf_score],
  ];
  return all
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([label, value]) => [label, formatScore(value)]);
}

function documentHref(source: SourceRef): string {
  const params = new URLSearchParams();
  if (source.page_number) params.set("page", String(source.page_number));
  params.set("chunk", source.chunk_id);
  return `/documents/${source.document_id}?${params}`;
}

function location(source: SourceRef): string | null {
  const parts = [
    source.page_number
      ? source.page_end && source.page_end !== source.page_number
        ? `Pages ${source.page_number}-${source.page_end}`
        : `Page ${source.page_number}`
      : null,
    source.section_title,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

function SourceItem({
  source,
  selected,
  scrollRef,
}: {
  source: SourceRef;
  selected: boolean;
  scrollRef?: (node: HTMLElement | null) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const redacted = source.redacted || source.excerpt === null;
  const where = location(source);
  const values = scores(source);
  return (
    <article
      ref={scrollRef}
      id={`source-${source.source_id}`}
      data-testid="source-item"
      data-source-id={source.source_id}
      data-cited={source.cited}
      data-redacted={redacted || undefined}
      aria-label={`Source ${source.source_id}`}
      className={cn(
        "rounded-lg border bg-white p-3",
        selected ? "border-accent-500 ring-2 ring-accent-200" : "border-slate-200",
      )}
    >
      <div className="flex items-start gap-2">
        <span className="mt-0.5 inline-flex h-5 min-w-6 items-center justify-center rounded-md bg-accent-50 px-1.5 text-[11px] font-semibold text-accent-800 ring-1 ring-accent-200 ring-inset">
          {source.source_id}
        </span>
        <div className="min-w-0 flex-1">
          {redacted ? (
            <p className="text-sm font-medium text-slate-500">Restricted source</p>
          ) : (
            <Link
              href={documentHref(source)}
              className="block text-sm font-medium break-words text-slate-900 hover:text-accent-700 hover:underline"
            >
              {source.document_title}
            </Link>
          )}
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-slate-500">
            {source.file_type ? <span>{fileTypeLabel(source.file_type)}</span> : null}
            {where ? <span>{where}</span> : null}
          </p>
        </div>
        <Badge tone={source.cited ? "success" : "neutral"}>{source.cited ? "Cited" : "Not cited"}</Badge>
      </div>

      {redacted ? (
        <p className="mt-2 flex items-start gap-2 rounded-md bg-slate-50 p-2 text-xs text-slate-600">
          <EyeOff aria-hidden className="mt-0.5 size-3.5 shrink-0" />
          Your access to this document has changed, so the passage is hidden.
        </p>
      ) : (
        <>
          <blockquote
            className={cn(
              "mt-2 border-l-2 border-slate-200 pl-3 text-[13px] leading-relaxed whitespace-pre-wrap text-slate-700",
              !expanded && "line-clamp-6",
            )}
          >
            {source.excerpt}
          </blockquote>
          {(source.excerpt?.length ?? 0) > 280 ? (
            <button
              type="button"
              className="mt-1 text-xs font-medium text-accent-700 hover:underline"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
            >
              {expanded ? "Show less" : "Show more"}
            </button>
          ) : null}
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-slate-500" title="Relevance scores from retrieval">
              {values.length ? values.map(([label, value]) => `${label} ${value}`).join(" · ") : "No relevance score"}
            </p>
            <Link href={documentHref(source)} className="text-xs font-medium text-accent-700 hover:underline">
              Open document
            </Link>
          </div>
        </>
      )}
    </article>
  );
}

/** The passages the model was given for one answer, with which of them it actually cited. */
export function SourcePanel({
  sources,
  selectedId,
  onClose,
  showHeader = true,
}: {
  sources: SourceRef[];
  selectedId: string | null;
  onClose: () => void;
  /** False when a drawer already provides the title and close button. */
  showHeader?: boolean;
}) {
  const scrollTo = useCallback((node: HTMLElement | null) => node?.scrollIntoView({ block: "nearest" }), []);
  const cited = sources.filter((s) => s.cited).length;
  return (
    <section aria-label="Sources" data-testid="source-panel" className="flex h-full min-h-0 flex-col bg-slate-50">
      <header className="flex items-center justify-between gap-2 border-b border-slate-200 bg-white px-4 py-3">
        <div>
          {showHeader ? <h2 className="text-sm font-semibold text-slate-900">Sources</h2> : null}
          <p className="text-xs text-slate-500">
            {sources.length} passage{sources.length === 1 ? "" : "s"} used · {cited} cited
          </p>
        </div>
        {showHeader ? (
          <IconButton label="Close sources" onClick={onClose}>
            <X aria-hidden className="size-4" />
          </IconButton>
        ) : null}
      </header>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        {sources.length === 0 ? (
          <p className="py-8 text-center text-sm text-slate-500">No passages were retrieved for this answer.</p>
        ) : (
          sources.map((source) => (
            <SourceItem
              key={source.source_id}
              source={source}
              selected={source.source_id === selectedId}
              scrollRef={source.source_id === selectedId ? scrollTo : undefined}
            />
          ))
        )}
      </div>
    </section>
  );
}
