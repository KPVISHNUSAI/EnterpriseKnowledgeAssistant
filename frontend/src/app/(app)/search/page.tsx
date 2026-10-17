"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";
import { ChevronDown, Search, SearchX } from "lucide-react";
import { ResultCard } from "@/components/search/result-card";
import { EMPTY_SCOPE, ScopePicker, type Scope } from "@/components/shared/scope-picker";
import { Button, LinkButton } from "@/components/ui/button";
import { EmptyState, ErrorNotice, PageHeader, SkeletonRows } from "@/components/ui/feedback";
import { Checkbox, Field, Input, Select } from "@/components/ui/form";
import { api } from "@/lib/api/endpoints";
import type { FileType, SearchMode, SearchRequest } from "@/lib/api/types";
import { FILE_TYPE_LABELS, formatMs } from "@/lib/format";
import { keys } from "@/lib/hooks/keys";
import { useUrlState } from "@/lib/hooks/url-state";
import { FILE_TYPES } from "@/lib/labels";
import { cn } from "@/lib/cn";

function buildRequest(
  query: string,
  scope: Scope,
  fileTypes: FileType[],
  mode: "" | SearchMode,
  rerank: "" | "on" | "off",
  topK: number,
): SearchRequest {
  return {
    query,
    top_k: topK,
    mode: mode || undefined,
    rerank: rerank ? rerank === "on" : undefined,
    filters: {
      workspace_ids: scope.workspaceIds.length ? scope.workspaceIds : undefined,
      document_ids: scope.documents.length ? scope.documents.map((d) => d.id) : undefined,
      file_types: fileTypes.length ? fileTypes : undefined,
    },
  };
}

export default function SearchPage() {
  const { params, update } = useUrlState();
  const input = useRef<HTMLInputElement>(null);
  // The text last submitted (from the URL on first load); filters below re-run it live.
  const [submitted, setSubmitted] = useState(() => params.get("q")?.trim() ?? "");
  const [scope, setScope] = useState<Scope>(EMPTY_SCOPE);
  const [fileTypes, setFileTypes] = useState<FileType[]>([]);
  const [mode, setMode] = useState<"" | SearchMode>("");
  const [rerank, setRerank] = useState<"" | "on" | "off">("");
  const [topK, setTopK] = useState(10);
  const [advanced, setAdvanced] = useState(false);

  const request = submitted ? buildRequest(submitted, scope, fileTypes, mode, rerank, topK) : null;
  const search = useQuery({
    queryKey: keys.search(request ?? {}),
    queryFn: ({ signal }) => api.search(request!, signal),
    enabled: request !== null,
    staleTime: 60_000,
    retry: false,
    placeholderData: keepPreviousData,
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const text = input.current?.value.trim() ?? "";
    if (!text) return;
    setSubmitted(text);
    update({ q: text });
    if (text === submitted) void search.refetch();
  }

  const data = search.data;
  return (
    <>
      <PageHeader
        title="Search"
        description="Find passages across the documents you can read. For a written answer with citations, ask in Chat."
        actions={<LinkButton href="/chat">Ask in Chat</LinkButton>}
      />
      <form role="search" aria-label="Search documents" onSubmit={onSubmit} className="mb-6">
        <div className="flex gap-2">
          <Field label="Search query" hideLabel className="flex-1">
            {(p) => (
              <Input
                {...p}
                ref={input}
                type="search"
                name="q"
                defaultValue={submitted}
                maxLength={2000}
                placeholder="What are you looking for?"
                className="h-11 text-base"
                autoFocus
                data-testid="search-input"
              />
            )}
          </Field>
          <Button type="submit" variant="primary" className="h-11 px-5" data-testid="search-submit">
            <Search aria-hidden className="size-4" /> Search
          </Button>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
          <ScopePicker value={scope} onChange={setScope} />
          <fieldset className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <legend className="sr-only">File types</legend>
            {FILE_TYPES.map((type) => (
              <label key={type} className="flex items-center gap-1.5 text-sm text-slate-700">
                <Checkbox
                  checked={fileTypes.includes(type)}
                  onChange={(event) =>
                    setFileTypes((list) => (event.target.checked ? [...list, type] : list.filter((t) => t !== type)))
                  }
                />
                {FILE_TYPE_LABELS[type]}
              </label>
            ))}
          </fieldset>
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={advanced}
            aria-controls="search-advanced"
            onClick={() => setAdvanced((v) => !v)}
          >
            Advanced{" "}
            <ChevronDown aria-hidden className={cn("size-3.5 transition-transform", advanced && "rotate-180")} />
          </Button>
        </div>

        {advanced ? (
          <div
            id="search-advanced"
            className="mt-3 grid gap-3 rounded-lg border border-slate-200 bg-white p-3 sm:grid-cols-3"
            data-testid="search-advanced"
          >
            <Field label="Retrieval mode">
              {(p) => (
                <Select
                  {...p}
                  value={mode}
                  onChange={(e) => setMode(e.target.value as "" | SearchMode)}
                  data-testid="search-mode"
                >
                  <option value="">Server default</option>
                  <option value="hybrid">Hybrid (vector + keyword)</option>
                  <option value="vector">Vector (semantic)</option>
                  <option value="keyword">Keyword (full text)</option>
                </Select>
              )}
            </Field>
            <Field label="Reranking">
              {(p) => (
                <Select
                  {...p}
                  value={rerank}
                  onChange={(e) => setRerank(e.target.value as "" | "on" | "off")}
                  data-testid="search-rerank"
                >
                  <option value="">Server default</option>
                  <option value="on">On</option>
                  <option value="off">Off</option>
                </Select>
              )}
            </Field>
            <Field label="Number of results">
              {(p) => (
                <Select {...p} value={topK} onChange={(e) => setTopK(Number(e.target.value))}>
                  {[5, 10, 20, 30, 50].map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>
        ) : null}
      </form>

      <div aria-live="polite" aria-busy={search.isFetching} data-testid="search-results">
        {!request ? (
          <EmptyState
            icon={<Search />}
            title="Search your documents"
            description="Enter a question or keywords. Results only include documents you are allowed to read."
          />
        ) : search.isError ? (
          <ErrorNotice error={search.error} title="Search failed" onRetry={() => search.refetch()} />
        ) : search.isPending ? (
          <SkeletonRows rows={4} />
        ) : data && data.results.length === 0 ? (
          <EmptyState
            icon={<SearchX />}
            title={`No results for “${data.query}”`}
            description="Try different words, remove filters, or switch the retrieval mode under Advanced. Documents that are still being indexed are not searchable yet."
          />
        ) : data ? (
          <div className={cn("space-y-3", search.isPlaceholderData && "opacity-60 transition-opacity")}>
            <p className="text-sm text-slate-600" data-testid="search-summary">
              {data.results.length} result{data.results.length === 1 ? "" : "s"} for “{data.query}” · {data.mode} mode ·{" "}
              {data.reranked ? "reranked" : "not reranked"}
              {typeof data.timings_ms.total === "number" ? ` · ${formatMs(data.timings_ms.total)}` : ""}
            </p>
            <ul className="space-y-3">
              {data.results.map((result) => (
                <li key={result.chunk_id}>
                  <ResultCard result={result} />
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </>
  );
}
