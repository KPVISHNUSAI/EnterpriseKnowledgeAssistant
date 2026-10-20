"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import { ChevronRight, Download, FileX, Pencil, RefreshCw, Trash2 } from "lucide-react";
import { Button, LinkButton, buttonClass } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { Alert, Badge, Card, EmptyState, ErrorNotice, Skeleton } from "@/components/ui/feedback";
import { Tab, TabList, TabPanel, Tabs } from "@/components/ui/tabs";
import { Time } from "@/components/ui/time";
import { ApiError } from "@/lib/api/errors";
import { documentFileUrl } from "@/lib/api/endpoints";
import type { DocumentDetail } from "@/lib/api/types";
import { fileTypeLabel, formatBytes, formatDateTime, formatNumber } from "@/lib/format";
import { useDeleteDocument, useDocument, useReindexDocument } from "@/lib/hooks/documents";
import { intParam, useUrlState } from "@/lib/hooks/url-state";
import { ExtractedText } from "./extracted-text";
import { FlaggedTextBadge, FlaggedTextNotice } from "./flagged-text";
import { RenameDialog } from "./rename-dialog";
import { ReplaceFileButton } from "./replace-file-button";
import { SharingEditor } from "./sharing-editor";
import { StatusBadge } from "./status-badge";

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium text-slate-500">{label}</dt>
      <dd className="mt-0.5 truncate text-sm text-slate-900">{children}</dd>
    </div>
  );
}

function Metadata({ doc }: { doc: DocumentDetail }) {
  const job = doc.latest_job;
  return (
    <Card className="p-4" data-testid="document-metadata">
      <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-4">
        <Meta label="File">{doc.original_filename}</Meta>
        <Meta label="Type">
          {fileTypeLabel(doc.file_type)} <span className="text-slate-500">({doc.mime_type})</span>
        </Meta>
        <Meta label="Size">{formatBytes(doc.file_size)}</Meta>
        <Meta label="Version">{doc.version}</Meta>
        <Meta label="Workspace">
          <Link href={`/workspaces/${doc.workspace_id}`} className="text-accent-700 hover:underline">
            {doc.workspace_name}
          </Link>
        </Meta>
        <Meta label="Uploaded by">{doc.uploaded_by?.full_name ?? "-"}</Meta>
        <Meta label="Uploaded">{formatDateTime(doc.created_at)}</Meta>
        <Meta label="Last updated">{formatDateTime(doc.updated_at)}</Meta>
        <Meta label="Pages">{doc.page_count ? formatNumber(doc.page_count) : "-"}</Meta>
        <Meta label="Chunks">{formatNumber(doc.chunk_count)}</Meta>
        <Meta label="Indexed">{doc.indexed_at ? <Time iso={doc.indexed_at} /> : "-"}</Meta>
        <Meta label="Content hash">
          <code className="font-mono text-xs" title={doc.content_hash}>
            {doc.content_hash.slice(0, 12)}
          </code>
        </Meta>
        {job ? (
          <>
            <Meta label="Latest ingestion job">
              {job.status} (attempt {job.attempts} of {job.max_attempts})
            </Meta>
            <Meta label="Job started">{job.started_at ? formatDateTime(job.started_at) : "-"}</Meta>
            <Meta label="Job finished">{job.completed_at ? formatDateTime(job.completed_at) : "-"}</Meta>
            {job.error_code ? <Meta label="Error code">{job.error_code}</Meta> : null}
          </>
        ) : null}
      </dl>
    </Card>
  );
}

export function DocumentView({ id }: { id: string }) {
  const router = useRouter();
  const confirm = useConfirm();
  const { params, update } = useUrlState();
  const query = useDocument(id);
  const reindex = useReindexDocument();
  const remove = useDeleteDocument();
  const [renaming, setRenaming] = useState(false);

  if (query.isPending) {
    return (
      <div className="space-y-4" role="status" aria-label="Loading document">
        <Skeleton className="h-8 w-1/2" />
        <Skeleton className="h-28" />
        <Skeleton className="h-96" />
      </div>
    );
  }
  if (query.isError) {
    if (query.error instanceof ApiError && query.error.status === 404) {
      return (
        <EmptyState
          icon={<FileX />}
          title="Document not found"
          description="It may have been deleted, or you may not have access to it."
          action={<LinkButton href="/documents">Back to documents</LinkButton>}
        />
      );
    }
    return <ErrorNotice error={query.error} title="Could not load the document" onRetry={() => query.refetch()} />;
  }

  const doc = query.data;
  const isPdf = doc.file_type === "pdf";
  const page = intParam(params.get("page"), 0) || undefined;
  const chunkId = params.get("chunk") ?? undefined;
  const tabs = [...(isPdf ? ["preview"] : []), "text", ...(doc.can_manage ? ["sharing"] : [])];
  const requested = params.get("tab") ?? "";
  const tab = tabs.includes(requested) ? requested : isPdf && !chunkId ? "preview" : "text";
  const active = doc.processing_status === "queued" || doc.processing_status === "processing";

  async function onDelete() {
    const ok = await confirm({
      title: "Delete this document?",
      description: `“${doc.title}” will be removed together with its extracted text, embeddings and stored file. This cannot be undone.`,
      confirmLabel: "Delete document",
      destructive: true,
    });
    if (ok) remove.mutate(doc.id, { onSuccess: () => router.replace("/documents") });
  }

  return (
    <div className="space-y-5">
      <title>{`${doc.title} | Knowledge Assistant`}</title>
      <nav aria-label="Breadcrumb" className="flex items-center gap-1 text-sm text-slate-500">
        <Link href="/documents" className="hover:text-slate-900 hover:underline">
          Documents
        </Link>
        <ChevronRight aria-hidden className="size-3.5" />
        <span className="truncate text-slate-700" aria-current="page">
          {doc.title}
        </span>
      </nav>

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight break-words text-slate-900" data-testid="document-title">
            {doc.title}
          </h1>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StatusBadge status={doc.processing_status} error={doc.processing_error} />
            <Badge>{fileTypeLabel(doc.file_type)}</Badge>
            <Badge tone={doc.visibility === "restricted" ? "warning" : "neutral"}>
              {doc.visibility === "restricted" ? "Restricted" : "Workspace-wide"}
            </Badge>
            <FlaggedTextBadge doc={doc} />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <a href={documentFileUrl(doc.id)} download className={buttonClass("secondary")}>
            <Download aria-hidden className="size-4" /> Download
          </a>
          {doc.can_manage ? (
            <>
              <ReplaceFileButton documentId={doc.id} />
              <Button onClick={() => setRenaming(true)}>
                <Pencil aria-hidden className="size-4" /> Rename
              </Button>
              <Button
                variant={doc.processing_status === "failed" ? "primary" : "secondary"}
                disabled={active}
                loading={reindex.isPending}
                onClick={() => reindex.mutate(doc.id)}
                data-testid="reindex"
              >
                <RefreshCw aria-hidden className="size-4" />{" "}
                {doc.processing_status === "failed" ? "Retry indexing" : "Re-index"}
              </Button>
              <Button variant="danger-outline" onClick={onDelete} loading={remove.isPending}>
                <Trash2 aria-hidden className="size-4" /> Delete
              </Button>
            </>
          ) : null}
        </div>
      </div>

      {doc.processing_status === "failed" ? (
        <Alert tone="danger" title="Indexing failed" data-testid="ingestion-failure">
          {doc.processing_error ?? doc.latest_job?.error_message ?? "The document could not be processed."}
          {doc.can_manage ? " You can retry indexing, or replace the file with a corrected version." : null}
        </Alert>
      ) : null}
      {active ? (
        <Alert
          tone="info"
          title={doc.processing_status === "processing" ? "Indexing in progress" : "Waiting to be indexed"}
        >
          This page updates automatically. The document becomes searchable when indexing finishes.
        </Alert>
      ) : null}

      <FlaggedTextNotice doc={doc} />

      <Metadata doc={doc} />

      <Tabs value={tab} onValueChange={(next) => update({ tab: next })}>
        <TabList aria-label="Document views">
          {isPdf ? <Tab value="preview">Preview</Tab> : null}
          <Tab value="text">Extracted text</Tab>
          {doc.can_manage ? <Tab value="sharing">Sharing</Tab> : null}
        </TabList>
        {isPdf ? (
          <TabPanel value="preview">
            <iframe
              title={`PDF preview of ${doc.title}`}
              src={documentFileUrl(doc.id, { inline: true, page })}
              className="h-[75vh] w-full rounded-lg border border-slate-200 bg-white"
              data-testid="pdf-preview"
            />
          </TabPanel>
        ) : null}
        <TabPanel value="text">
          {!isPdf ? (
            <p className="mb-3 text-sm text-slate-600">
              This file type is shown as extracted text. Use Download to get the original file.
            </p>
          ) : null}
          <ExtractedText
            documentId={doc.id}
            status={doc.processing_status}
            chunkCount={doc.chunk_count}
            indexedAt={doc.indexed_at ?? null}
            chunkId={chunkId}
            page={page}
            flaggedIncluded={doc.flagged_text_included}
            onShowInPdf={isPdf ? (target) => update({ tab: "preview", page: target, chunk: null }) : undefined}
          />
        </TabPanel>
        {doc.can_manage ? (
          <TabPanel value="sharing">
            <SharingEditor key={JSON.stringify([doc.visibility, doc.permissions])} doc={doc} />
          </TabPanel>
        ) : null}
      </Tabs>

      <RenameDialog documentId={doc.id} title={doc.title} open={renaming} onOpenChange={setRenaming} />
    </div>
  );
}
