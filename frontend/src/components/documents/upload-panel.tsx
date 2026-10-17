"use client";

import Link from "next/link";
import { useRef, useState, type DragEvent } from "react";
import { AlertCircle, CheckCircle2, FileText, RefreshCw, UploadCloud, X } from "lucide-react";
import { Button, IconButton } from "@/components/ui/button";
import { Alert, Card } from "@/components/ui/feedback";
import { Field, Select } from "@/components/ui/form";
import { ApiError, errorMessage, errorRequestId, isAbortError } from "@/lib/api/errors";
import { uploadDocument } from "@/lib/api/upload";
import type { DocumentVisibility } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { formatBytes } from "@/lib/format";
import { useInvalidateAfterUpload } from "@/lib/hooks/documents";
import { useWorkspaces } from "@/lib/hooks/workspaces";

const EXTENSIONS = [".pdf", ".docx", ".txt", ".md", ".markdown"];
const CONCURRENCY = 2;

interface UploadItem {
  id: number;
  file: File;
  workspaceId: string;
  visibility: DocumentVisibility;
  status: "waiting" | "uploading" | "done" | "error";
  progress: number; // 0..1 of the request body sent
  error: unknown;
  /** The created document, or the existing one when the server reports a duplicate (409). */
  documentId: string | null;
}

function validate(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!EXTENSIONS.some((ext) => name.endsWith(ext))) return "Unsupported file type. Use PDF, DOCX, TXT or Markdown.";
  if (file.size === 0) return "This file is empty.";
  return null;
}

function duplicateOf(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const id = (error.details as { document_id?: unknown } | null)?.document_id;
  return typeof id === "string" ? id : null;
}

export function UploadPanel({ defaultWorkspaceId }: { defaultWorkspaceId?: string }) {
  const workspaces = useWorkspaces();
  const uploadable = workspaces.data?.filter((ws) => ws.can_upload) ?? [];
  const [chosenWorkspace, setChosenWorkspace] = useState("");
  const [visibility, setVisibility] = useState<DocumentVisibility>("workspace");
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const controllers = useRef(new Map<number, AbortController>());
  const nextId = useRef(0);
  const refreshLists = useInvalidateAfterUpload();

  const workspaceId =
    uploadable.find((ws) => ws.id === chosenWorkspace)?.id ??
    uploadable.find((ws) => ws.id === defaultWorkspaceId)?.id ??
    uploadable[0]?.id ??
    "";

  const patch = (id: number, changes: Partial<UploadItem>) =>
    setItems((list) => list.map((item) => (item.id === id ? { ...item, ...changes } : item)));

  async function send(item: UploadItem) {
    const controller = new AbortController();
    controllers.current.set(item.id, controller);
    patch(item.id, { status: "uploading", progress: 0, error: null, documentId: null });
    try {
      const result = await uploadDocument(
        { file: item.file, workspaceId: item.workspaceId, visibility: item.visibility },
        { signal: controller.signal, onProgress: (progress) => patch(item.id, { progress }) },
      );
      patch(item.id, { status: "done", progress: 1, documentId: result.document.id });
      void refreshLists();
    } catch (error) {
      patch(item.id, {
        status: "error",
        error: isAbortError(error) ? new Error("Upload cancelled.") : error,
        documentId: duplicateOf(error),
      });
    } finally {
      controllers.current.delete(item.id);
    }
  }

  async function run(batch: UploadItem[]) {
    const queue = [...batch];
    const worker = async () => {
      for (let item = queue.shift(); item; item = queue.shift()) await send(item);
    };
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, queue.length) }, worker));
    void refreshLists();
  }

  function addFiles(files: FileList | File[]) {
    if (!workspaceId) return;
    const added: UploadItem[] = Array.from(files).map((file) => {
      const problem = validate(file);
      return {
        id: nextId.current++,
        file,
        workspaceId,
        visibility,
        status: problem ? "error" : "waiting",
        progress: 0,
        error: problem ? new Error(problem) : null,
        documentId: null,
      };
    });
    setItems((list) => [...list, ...added]);
    void run(added.filter((item) => item.status === "waiting"));
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    addFiles(event.dataTransfer.files);
  }

  if (workspaces.isPending) return null;
  if (uploadable.length === 0) {
    return (
      <Alert tone="info" className="mb-4">
        You do not have upload access to any workspace. Ask a workspace owner to make you an editor.
      </Alert>
    );
  }

  const finished = items.filter((item) => item.status === "done" || item.status === "error");
  return (
    <Card className="mb-6 p-4">
      <div className="mb-3 grid gap-3 sm:grid-cols-2">
        <Field label="Upload to workspace">
          {(p) => (
            <Select
              {...p}
              value={workspaceId}
              onChange={(e) => setChosenWorkspace(e.target.value)}
              data-testid="upload-workspace"
            >
              {uploadable.map((ws) => (
                <option key={ws.id} value={ws.id}>
                  {ws.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <fieldset>
          <legend className="mb-1 text-sm font-medium text-slate-700">Who can read these documents</legend>
          <div className="flex flex-wrap gap-x-5 gap-y-1 pt-1.5 text-sm" data-testid="upload-visibility">
            {(
              [
                ["workspace", "Workspace members"],
                ["restricted", "Only people I share with"],
              ] as const
            ).map(([value, label]) => (
              <label key={value} className="flex items-center gap-2">
                <input
                  type="radio"
                  name="upload-visibility"
                  value={value}
                  checked={visibility === value}
                  onChange={() => setVisibility(value)}
                  className="accent-accent-600"
                />
                {label}
              </label>
            ))}
          </div>
        </fieldset>
      </div>

      <div
        data-testid="upload-dropzone"
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cn(
          "flex flex-col items-center gap-3 rounded-lg border-2 border-dashed px-4 py-4 text-center transition-colors sm:flex-row sm:text-left",
          dragging ? "border-accent-500 bg-accent-50" : "border-slate-300 bg-slate-50/50",
        )}
      >
        <UploadCloud aria-hidden className="size-7 shrink-0 text-slate-400" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-slate-800">Drag and drop files here</p>
          <p className="text-xs text-slate-500">PDF, DOCX, TXT or Markdown. You can select several files.</p>
        </div>
        <Button onClick={() => input.current?.click()}>Choose files</Button>
        <input
          ref={input}
          type="file"
          multiple
          accept={EXTENSIONS.join(",")}
          className="sr-only"
          tabIndex={-1}
          aria-label="Upload files"
          data-testid="upload-input"
          onChange={(event) => {
            if (event.target.files) addFiles(event.target.files);
            event.target.value = ""; // allow choosing the same file again
          }}
        />
      </div>

      {items.length > 0 ? (
        <div className="mt-4">
          <ul className="divide-y divide-slate-100" aria-label="Uploads">
            {items.map((item) => (
              <UploadRow
                key={item.id}
                item={item}
                onCancel={() => controllers.current.get(item.id)?.abort()}
                onRetry={() => void run([item])}
                onRemove={() => setItems((list) => list.filter((entry) => entry.id !== item.id))}
              />
            ))}
          </ul>
          {finished.length > 0 ? (
            <Button
              size="sm"
              variant="ghost"
              className="mt-2"
              onClick={() =>
                setItems((list) => list.filter((item) => item.status === "waiting" || item.status === "uploading"))
              }
            >
              Clear finished
            </Button>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

function UploadRow({
  item,
  onCancel,
  onRetry,
  onRemove,
}: {
  item: UploadItem;
  onCancel: () => void;
  onRetry: () => void;
  onRemove: () => void;
}) {
  const percent = Math.round(item.progress * 100);
  const processing = item.status === "uploading" && percent >= 100;
  const duplicate = item.status === "error" && item.documentId;
  const requestId = errorRequestId(item.error);
  return (
    <li className="flex items-start gap-3 py-2.5" data-testid="upload-item" data-status={item.status}>
      <FileText aria-hidden className="mt-0.5 size-5 shrink-0 text-slate-400" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <p className="truncate text-sm font-medium text-slate-900">{item.file.name}</p>
          <span className="shrink-0 text-xs text-slate-500">{formatBytes(item.file.size)}</span>
        </div>
        {item.status === "uploading" ? (
          <>
            <div
              role="progressbar"
              aria-label={`Uploading ${item.file.name}`}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent}
              className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-200"
            >
              <div className="h-full rounded-full bg-accent-600 transition-[width]" style={{ width: `${percent}%` }} />
            </div>
            <p className="mt-1 text-xs text-slate-500">
              {processing ? "Processing on the server..." : `Uploading ${percent}%`}
            </p>
          </>
        ) : null}
        {item.status === "waiting" ? <p className="mt-1 text-xs text-slate-500">Waiting...</p> : null}
        {item.status === "done" ? (
          <p className="mt-1 flex items-center gap-1 text-xs text-emerald-700">
            <CheckCircle2 aria-hidden className="size-3.5" /> Uploaded and queued for indexing.{" "}
            {item.documentId ? (
              <Link href={`/documents/${item.documentId}`} className="underline underline-offset-2">
                Open
              </Link>
            ) : null}
          </p>
        ) : null}
        {item.status === "error" ? (
          <p className="mt-1 flex flex-wrap items-center gap-x-1 text-xs text-red-700" role="alert">
            <AlertCircle aria-hidden className="size-3.5" />
            {duplicate ? "This file already exists in the workspace." : errorMessage(item.error)}
            {duplicate ? (
              <Link href={`/documents/${item.documentId}`} className="underline underline-offset-2">
                View existing document
              </Link>
            ) : null}
            {requestId ? <span className="text-red-700/70">(Request ID: {requestId})</span> : null}
          </p>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center">
        {item.status === "uploading" ? (
          <Button size="sm" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        ) : null}
        {item.status === "error" && !duplicate && validate(item.file) === null ? (
          <IconButton label={`Retry ${item.file.name}`} onClick={onRetry}>
            <RefreshCw aria-hidden className="size-4" />
          </IconButton>
        ) : null}
        {item.status !== "uploading" ? (
          <IconButton label={`Remove ${item.file.name} from the list`} onClick={onRemove}>
            <X aria-hidden className="size-4" />
          </IconButton>
        ) : null}
      </div>
    </li>
  );
}
