"use client";

import { useCallback, useEffect } from "react";
import { FileSearch } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Alert, EmptyState, ErrorNotice, SkeletonRows, Spinner } from "@/components/ui/feedback";
import type { Chunk, DocumentStatus } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { useDocumentChunks } from "@/lib/hooks/documents";
import { formatNumber } from "@/lib/format";
import { FLAG_RULE_LABELS } from "@/lib/labels";
import { FlaggedContent } from "./flagged-text";

const coversPage = (chunk: Chunk, page: number) =>
  chunk.page_number !== null &&
  chunk.page_number !== undefined &&
  chunk.page_number <= page &&
  page <= (chunk.page_end ?? chunk.page_number);

/**
 * The extracted text of the indexed version, chunk by chunk, grouped by page/section.
 * `chunkId` highlights one chunk (a citation target); `page` scrolls to the first chunk on that page.
 * Pages of chunks are fetched on demand until the target has been found.
 */
export function ExtractedText({
  documentId,
  status,
  chunkCount,
  indexedAt,
  chunkId,
  page,
  flaggedIncluded = false,
  onShowInPdf,
}: {
  documentId: string;
  status: DocumentStatus;
  /** Chunks of the last indexed version; text stays available while a new version is processed. */
  chunkCount: number;
  indexedAt: string | null;
  chunkId?: string;
  page?: number;
  /** A document manager let the flagged text into answers. */
  flaggedIncluded?: boolean;
  onShowInPdf?: (page: number) => void;
}) {
  const hasText = status === "indexed" || chunkCount > 0;
  const query = useDocumentChunks(documentId, indexedAt, hasText);
  const chunks = query.data?.pages.flatMap((p) => p.items) ?? [];
  const total = query.data?.pages[0]?.total ?? 0;

  const target = chunks.find((c) => (chunkId ? c.id === chunkId : page ? coversPage(c, page) : false));
  const wanted = Boolean(chunkId || page);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = query;
  const loadedPages = query.data?.pages.length ?? 0;
  useEffect(() => {
    // loadedPages: a fast response can finish between two renders, so "fetching" is never observed.
    if (wanted && !target && hasNextPage && !isFetchingNextPage) void fetchNextPage();
  }, [wanted, target, hasNextPage, isFetchingNextPage, loadedPages, fetchNextPage]);

  // Scroll the target into view once, when its element first mounts.
  const scrollTo = useCallback((node: HTMLElement | null) => node?.scrollIntoView({ block: "center" }), []);

  if (!hasText) {
    return status === "failed" ? (
      <Alert tone="warning">No text is available because indexing failed. Retry indexing to extract it.</Alert>
    ) : (
      <Alert tone="info">The text will appear here once indexing has finished.</Alert>
    );
  }
  if (query.isError) return <ErrorNotice error={query.error} onRetry={() => query.refetch()} />;
  if (query.isPending) return <SkeletonRows rows={5} />;
  if (chunks.length === 0) {
    return (
      <EmptyState
        icon={<FileSearch />}
        title="No extracted text"
        description="This document produced no text to index."
      />
    );
  }

  const sectionOf = (chunk?: Chunk) => (chunk ? chunk.heading_path || chunk.section_title || null : null);
  return (
    <div data-testid="extracted-text">
      {status !== "indexed" ? (
        <Alert tone="info" className="mb-3">
          {status === "failed"
            ? "Indexing the latest file failed. This is the text of the previous version."
            : "The latest file is still being indexed. This is the text of the previous version."}
        </Alert>
      ) : null}
      <p className="mb-3 text-sm text-slate-600">
        {formatNumber(total)} {total === 1 ? "chunk" : "chunks"}. Neighbouring chunks overlap slightly, so some
        sentences appear twice.
      </p>
      {chunkId && !target && !hasNextPage ? (
        <Alert tone="warning" className="mb-3">
          The cited passage is not part of the current version of this document. It may have been replaced.
        </Alert>
      ) : null}
      <ol className="space-y-3">
        {chunks.map((chunk, index) => {
          const previous = chunks[index - 1];
          const pageNumber = chunk.page_number;
          const pageChanged = pageNumber !== previous?.page_number;
          const sectionLabel = sectionOf(chunk);
          const flagRule = chunk.flags?.[0]?.rule ?? chunk.section_flag;
          const sectionChanged = sectionLabel !== sectionOf(previous);
          const highlighted = Boolean(chunkId) && chunk.id === chunkId;
          const isTarget = target?.id === chunk.id;
          return (
            <li key={chunk.id}>
              {pageChanged && pageNumber ? (
                <h3 className="mt-5 mb-2 flex items-center gap-2 text-xs font-semibold tracking-wide text-slate-500 uppercase">
                  Page {pageNumber}
                  {onShowInPdf ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-6 px-2 text-xs normal-case"
                      onClick={() => onShowInPdf(pageNumber)}
                    >
                      Show in PDF
                    </Button>
                  ) : null}
                </h3>
              ) : null}
              {sectionChanged && sectionLabel ? (
                <p className="mb-1 text-sm font-medium text-slate-700">
                  {chunk.section_flag ? (
                    <mark
                      className="rounded-sm bg-amber-200/70 text-amber-950 ring-1 ring-amber-400"
                      title={FLAG_RULE_LABELS[chunk.section_flag]}
                      data-testid="flagged-heading"
                    >
                      {sectionLabel}
                    </mark>
                  ) : (
                    sectionLabel
                  )}
                </p>
              ) : null}
              <article
                ref={isTarget ? scrollTo : undefined}
                id={`chunk-${chunk.id}`}
                data-testid="chunk"
                data-highlighted={highlighted || undefined}
                aria-label={
                  highlighted ? `Cited passage, chunk ${chunk.chunk_index + 1}` : `Chunk ${chunk.chunk_index + 1}`
                }
                className={cn(
                  "rounded-lg border bg-white p-3",
                  highlighted ? "border-amber-400 bg-amber-50 ring-2 ring-amber-300" : "border-slate-200",
                )}
              >
                <p className="mb-1 text-xs text-slate-500">
                  Chunk {chunk.chunk_index + 1} · {formatNumber(chunk.token_count)} tokens
                  {highlighted ? <strong className="ml-2 text-amber-800">Cited passage</strong> : null}
                  {flagRule ? (
                    <strong className={cn("ml-2", flaggedIncluded ? "text-red-700" : "text-amber-800")}>
                      {flaggedIncluded ? "Included in answers" : "Left out of answers"}:{" "}
                      {FLAG_RULE_LABELS[flagRule].replace(/^./, (first) => first.toLowerCase())}
                    </strong>
                  ) : null}
                </p>
                <p className="text-sm leading-relaxed whitespace-pre-wrap text-slate-800">
                  <FlaggedContent content={chunk.content} flags={chunk.flags ?? []} />
                </p>
              </article>
            </li>
          );
        })}
      </ol>
      {hasNextPage ? (
        <div className="mt-4 flex justify-center">
          <Button onClick={() => void fetchNextPage()} disabled={isFetchingNextPage}>
            {isFetchingNextPage ? <Spinner /> : null} Load more
          </Button>
        </div>
      ) : null}
    </div>
  );
}
