"use client";

import Link from "next/link";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Download,
  ExternalLink,
  MoreHorizontal,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { Button, IconButton } from "@/components/ui/button";
import { Badge } from "@/components/ui/feedback";
import { Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { Time } from "@/components/ui/time";
import { useConfirm } from "@/components/ui/confirm";
import { documentFileUrl } from "@/lib/api/endpoints";
import type { DocumentItem, DocumentSort, SortOrder } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { fileTypeLabel, formatBytes } from "@/lib/format";
import { useDeleteDocument, useReindexDocument } from "@/lib/hooks/documents";
import { FlaggedTextBadge } from "./flagged-text";
import { StatusBadge } from "./status-badge";

function SortHeader({
  label,
  column,
  sort,
  order,
  onSort,
  className,
}: {
  label: string;
  column: DocumentSort;
  sort: DocumentSort;
  order: SortOrder;
  onSort: (column: DocumentSort) => void;
  className?: string;
}) {
  const active = sort === column;
  const Icon = !active ? ArrowUpDown : order === "asc" ? ArrowUp : ArrowDown;
  return (
    <th
      scope="col"
      aria-sort={active ? (order === "asc" ? "ascending" : "descending") : undefined}
      className={cn("px-3 py-2 text-left font-medium", className)}
    >
      <button
        type="button"
        onClick={() => onSort(column)}
        className="inline-flex items-center gap-1 rounded-sm font-medium text-slate-600 hover:text-slate-900"
      >
        {label}
        <Icon aria-hidden className={cn("size-3.5", active ? "text-accent-700" : "text-slate-400")} />
      </button>
    </th>
  );
}

function RowActions({ doc }: { doc: DocumentItem }) {
  const confirm = useConfirm();
  const reindex = useReindexDocument();
  const remove = useDeleteDocument();

  async function onDelete() {
    const ok = await confirm({
      title: "Delete this document?",
      description: (
        <>
          “{doc.title}” will be removed together with its extracted text, embeddings and stored file. This cannot be
          undone.
        </>
      ),
      confirmLabel: "Delete document",
      destructive: true,
    });
    if (ok) remove.mutate(doc.id);
  }

  return (
    <Menu>
      <MenuTrigger asChild>
        <IconButton label={`Actions for ${doc.title}`} data-testid="document-actions">
          <MoreHorizontal aria-hidden className="size-4" />
        </IconButton>
      </MenuTrigger>
      <MenuContent>
        <MenuItem asChild>
          <Link href={`/documents/${doc.id}`}>
            <ExternalLink aria-hidden className="size-4 text-slate-500" /> Open
          </Link>
        </MenuItem>
        <MenuItem asChild>
          <a href={documentFileUrl(doc.id)} download>
            <Download aria-hidden className="size-4 text-slate-500" /> Download original
          </a>
        </MenuItem>
        {doc.can_manage ? (
          <>
            <MenuItem
              disabled={doc.processing_status === "queued" || doc.processing_status === "processing"}
              onSelect={() => reindex.mutate(doc.id)}
            >
              <RefreshCw aria-hidden className="size-4 text-slate-500" /> Re-index
            </MenuItem>
            <MenuSeparator />
            <MenuItem destructive onSelect={onDelete}>
              <Trash2 aria-hidden className="size-4" /> Delete
            </MenuItem>
          </>
        ) : null}
      </MenuContent>
    </Menu>
  );
}

export function DocumentsTable({
  documents,
  sort,
  order,
  onSort,
}: {
  documents: DocumentItem[];
  sort: DocumentSort;
  order: SortOrder;
  onSort: (column: DocumentSort) => void;
}) {
  const reindex = useReindexDocument();
  return (
    <div className="relative overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="w-full min-w-[40rem] text-sm" data-testid="documents-table">
        <caption className="sr-only">Documents</caption>
        <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-600">
          <tr>
            <SortHeader label="Title" column="title" sort={sort} order={order} onSort={onSort} />
            <th scope="col" className="hidden px-3 py-2 text-left font-medium md:table-cell">
              Workspace
            </th>
            <th scope="col" className="hidden px-3 py-2 text-left font-medium sm:table-cell">
              Type
            </th>
            <SortHeader
              label="Size"
              column="file_size"
              sort={sort}
              order={order}
              onSort={onSort}
              className="hidden sm:table-cell"
            />
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Status
            </th>
            <th scope="col" className="hidden px-3 py-2 text-left font-medium lg:table-cell">
              Uploaded by
            </th>
            <SortHeader
              label="Updated"
              column="updated_at"
              sort={sort}
              order={order}
              onSort={onSort}
              className="hidden md:table-cell"
            />
            <th scope="col" className="w-10 px-3 py-2">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {documents.map((doc) => (
            <tr key={doc.id} data-testid="document-row" className="align-top hover:bg-slate-50/60">
              <td className="max-w-80 px-3 py-3">
                <Link
                  href={`/documents/${doc.id}`}
                  className="font-medium text-slate-900 hover:text-accent-700 hover:underline"
                >
                  {doc.title}
                </Link>
                {doc.visibility === "restricted" ? (
                  <Badge tone="warning" className="ml-2" title="Only people it is shared with can read this document">
                    Restricted
                  </Badge>
                ) : null}
                {doc.original_filename !== doc.title ? (
                  <p className="truncate text-xs text-slate-500">{doc.original_filename}</p>
                ) : null}
              </td>
              <td className="hidden px-3 py-3 text-slate-700 md:table-cell">{doc.workspace_name}</td>
              <td className="hidden px-3 py-3 sm:table-cell">
                <Badge>{fileTypeLabel(doc.file_type)}</Badge>
              </td>
              <td className="hidden px-3 py-3 whitespace-nowrap text-slate-700 sm:table-cell">
                {formatBytes(doc.file_size)}
              </td>
              <td className="px-3 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusBadge status={doc.processing_status} error={doc.processing_error} />
                  <FlaggedTextBadge doc={doc} />
                  {doc.processing_status === "failed" && doc.can_manage ? (
                    <Button
                      size="sm"
                      variant="secondary"
                      className="h-6 px-2 text-xs"
                      onClick={() => reindex.mutate(doc.id)}
                      disabled={reindex.isPending && reindex.variables === doc.id}
                      aria-label={`Retry indexing ${doc.title}`}
                    >
                      <RefreshCw aria-hidden className="size-3" /> Retry
                    </Button>
                  ) : null}
                </div>
                {doc.processing_status === "failed" && doc.processing_error ? (
                  <p className="mt-1 max-w-64 text-xs text-red-700">{doc.processing_error}</p>
                ) : null}
              </td>
              <td className="hidden px-3 py-3 whitespace-nowrap text-slate-700 lg:table-cell">
                {doc.uploaded_by?.full_name ?? "-"}
              </td>
              <td className="hidden px-3 py-3 whitespace-nowrap text-slate-600 md:table-cell">
                <Time iso={doc.updated_at} />
              </td>
              <td className="px-3 py-3">
                <RowActions doc={doc} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
