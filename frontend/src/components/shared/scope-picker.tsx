"use client";

import { useState } from "react";
import { ChevronDown, Filter, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorNotice, SkeletonRows } from "@/components/ui/feedback";
import { Checkbox, Input } from "@/components/ui/form";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useDocuments } from "@/lib/hooks/documents";
import { useDebouncedCallback } from "@/lib/hooks/url-state";
import { useWorkspaces } from "@/lib/hooks/workspaces";
import { pluralize } from "@/lib/format";

/** Which workspaces/documents a chat question or search is limited to (empty = everything readable). */
export interface Scope {
  workspaceIds: string[];
  documents: Array<{ id: string; title: string }>;
}

export const EMPTY_SCOPE: Scope = { workspaceIds: [], documents: [] };

export function scopeSummary(scope: Scope): string {
  const parts = [
    scope.workspaceIds.length ? pluralize(scope.workspaceIds.length, "workspace") : null,
    scope.documents.length ? pluralize(scope.documents.length, "document") : null,
  ].filter(Boolean);
  return parts.length ? parts.join(", ") : "All documents";
}

export function ScopePicker({
  value,
  onChange,
  disabled,
}: {
  value: Scope;
  onChange: (scope: Scope) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const setDebounced = useDebouncedCallback((text: string) => setSearch(text), 250);
  const workspaces = useWorkspaces();
  // Only looked up once the picker is opened.
  const documents = useDocuments(
    { q: search || undefined, status: ["indexed"], sort: "title", order: "asc", page: 1, page_size: 20 },
    { enabled: open },
  );
  const selectedDocIds = new Set(value.documents.map((d) => d.id));
  const active = value.workspaceIds.length + value.documents.length > 0;

  function toggleWorkspace(id: string) {
    const ids = value.workspaceIds.includes(id)
      ? value.workspaceIds.filter((w) => w !== id)
      : [...value.workspaceIds, id];
    onChange({ ...value, workspaceIds: ids });
  }

  function toggleDocument(doc: { id: string; title: string }) {
    const docs = selectedDocIds.has(doc.id)
      ? value.documents.filter((d) => d.id !== doc.id)
      : [...value.documents, doc];
    onChange({ ...value, documents: docs });
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button size="sm" variant={active ? "primary" : "secondary"} disabled={disabled} data-testid="scope-selector">
          <Filter aria-hidden className="size-3.5" />
          <span className="max-w-48 truncate">{scopeSummary(value)}</span>
          <ChevronDown aria-hidden className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[min(26rem,calc(100vw-2rem))]">
        <div className="mb-2 flex items-center justify-between">
          <p className="text-sm font-semibold text-slate-900">Limit to</p>
          <Button size="sm" variant="ghost" disabled={!active} onClick={() => onChange(EMPTY_SCOPE)}>
            Clear
          </Button>
        </div>
        <div className="max-h-[60vh] space-y-4 overflow-y-auto">
          <fieldset>
            <legend className="mb-1 text-xs font-medium text-slate-500 uppercase">Workspaces</legend>
            {workspaces.isError ? (
              <ErrorNotice error={workspaces.error} onRetry={() => workspaces.refetch()} />
            ) : workspaces.isPending ? (
              <SkeletonRows rows={2} />
            ) : workspaces.data.length === 0 ? (
              <p className="text-sm text-slate-500">You are not a member of any workspace.</p>
            ) : (
              <ul className="space-y-1">
                {workspaces.data.map((ws) => (
                  <li key={ws.id}>
                    <label className="flex items-center gap-2 rounded-md px-1 py-1 text-sm hover:bg-slate-50">
                      <Checkbox checked={value.workspaceIds.includes(ws.id)} onChange={() => toggleWorkspace(ws.id)} />
                      <span className="truncate">{ws.name}</span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </fieldset>
          <fieldset>
            <legend className="mb-1 text-xs font-medium text-slate-500 uppercase">Documents</legend>
            {value.documents.length > 0 ? (
              <ul className="mb-2 flex flex-wrap gap-1.5">
                {value.documents.map((doc) => (
                  <li
                    key={doc.id}
                    className="flex max-w-full items-center gap-1 rounded-full bg-accent-50 py-0.5 pr-1 pl-2.5 text-xs text-accent-800"
                  >
                    <span className="truncate">{doc.title}</span>
                    <button
                      type="button"
                      aria-label={`Remove ${doc.title}`}
                      className="rounded-full p-0.5 hover:bg-accent-100"
                      onClick={() => toggleDocument(doc)}
                    >
                      <X aria-hidden className="size-3" />
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
            <Input
              placeholder="Search indexed documents"
              aria-label="Search indexed documents"
              onChange={(event) => setDebounced(event.target.value.trim())}
            />
            <div className="mt-2">
              {documents.isError ? (
                <ErrorNotice error={documents.error} onRetry={() => documents.refetch()} />
              ) : documents.isPending ? (
                <SkeletonRows rows={3} />
              ) : documents.data.items.length === 0 ? (
                <p className="py-2 text-sm text-slate-500">No indexed documents found.</p>
              ) : (
                <ul className="space-y-1">
                  {documents.data.items.map((doc) => (
                    <li key={doc.id}>
                      <label className="flex items-start gap-2 rounded-md px-1 py-1 text-sm hover:bg-slate-50">
                        <Checkbox
                          className="mt-0.5"
                          checked={selectedDocIds.has(doc.id)}
                          onChange={() => toggleDocument({ id: doc.id, title: doc.title })}
                        />
                        <span className="min-w-0">
                          <span className="block truncate">{doc.title}</span>
                          <span className="block truncate text-xs text-slate-500">{doc.workspace_name}</span>
                        </span>
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </fieldset>
        </div>
      </PopoverContent>
    </Popover>
  );
}
