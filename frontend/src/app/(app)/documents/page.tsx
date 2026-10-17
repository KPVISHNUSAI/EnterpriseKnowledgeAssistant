"use client";

import { FileText } from "lucide-react";
import { useSession } from "@/components/auth/session";
import { DocumentFilters, hasFilters, type DocumentFilterValues } from "@/components/documents/document-filters";
import { DocumentsTable } from "@/components/documents/documents-table";
import { UploadPanel } from "@/components/documents/upload-panel";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorNotice, PageHeader, SkeletonRows } from "@/components/ui/feedback";
import { Pagination } from "@/components/ui/pagination";
import type { DocumentListQuery, DocumentSort, SortOrder } from "@/lib/api/types";
import { useDocuments } from "@/lib/hooks/documents";
import { enumParam, intParam, useUrlState } from "@/lib/hooks/url-state";
import { DOCUMENT_STATUSES, FILE_TYPES } from "@/lib/labels";

const SORTS = ["created_at", "updated_at", "title", "file_size"] as const;
const PAGE_SIZES = [10, 20, 50, 100] as const;

export default function DocumentsPage() {
  const session = useSession();
  const { params, update } = useUrlState();

  const page = intParam(params.get("page"), 1);
  const pageSize = intParam(params.get("size"), 20);
  const sort = enumParam<DocumentSort>(params.get("sort"), SORTS, "updated_at");
  const order = enumParam<SortOrder>(params.get("order"), ["asc", "desc"], "desc");
  const filters: DocumentFilterValues = {
    q: params.get("q") ?? "",
    workspace: params.get("workspace") ?? "",
    status: enumParam(params.get("status"), ["", ...DOCUMENT_STATUSES], ""),
    type: enumParam(params.get("type"), ["", ...FILE_TYPES], ""),
    uploader: params.get("uploader") ?? "",
  };

  const query: DocumentListQuery = {
    page,
    page_size: PAGE_SIZES.includes(pageSize as (typeof PAGE_SIZES)[number]) ? pageSize : 20,
    sort,
    order,
    q: filters.q || undefined,
    workspace_id: filters.workspace ? [filters.workspace] : undefined,
    status: filters.status ? [filters.status as (typeof DOCUMENT_STATUSES)[number]] : undefined,
    file_type: filters.type ? [filters.type as (typeof FILE_TYPES)[number]] : undefined,
    uploaded_by: filters.uploader || undefined,
  };
  const documents = useDocuments(query);

  function onSort(column: DocumentSort) {
    if (column === sort) update({ order: order === "asc" ? "desc" : "asc", page: null });
    else update({ sort: column, order: column === "title" ? "asc" : "desc", page: null });
  }

  return (
    <>
      <PageHeader title="Documents" description="Everything you can read across your workspaces." />
      {session.capabilities.can_upload ? <UploadPanel defaultWorkspaceId={filters.workspace || undefined} /> : null}
      <DocumentFilters values={filters} onChange={(patch) => update({ ...patch, page: null })} />

      {documents.isError ? (
        <ErrorNotice error={documents.error} title="Could not load documents" onRetry={() => documents.refetch()} />
      ) : documents.isPending ? (
        <SkeletonRows rows={6} />
      ) : documents.data.items.length === 0 ? (
        hasFilters(filters) ? (
          <EmptyState
            icon={<FileText />}
            title="No documents match your filters"
            description="Try a different search or clear the filters."
            action={
              <Button
                onClick={() =>
                  update({ q: null, workspace: null, status: null, type: null, uploader: null, page: null })
                }
              >
                Clear filters
              </Button>
            }
          />
        ) : (
          <EmptyState
            icon={<FileText />}
            title="No documents yet"
            description={
              session.capabilities.can_upload
                ? "Upload your first document above to start searching and asking questions."
                : "Documents shared with you will appear here."
            }
          />
        )
      ) : (
        <div className="space-y-4">
          <div
            className={documents.isPlaceholderData ? "opacity-60 transition-opacity" : undefined}
            aria-busy={documents.isFetching}
          >
            <DocumentsTable documents={documents.data.items} sort={sort} order={order} onSort={onSort} />
          </div>
          <Pagination
            page={documents.data.page}
            pageSize={documents.data.page_size}
            total={documents.data.total}
            onPageChange={(next) => update({ page: next })}
            onPageSizeChange={(size) => update({ size, page: null })}
          />
        </div>
      )}
    </>
  );
}
