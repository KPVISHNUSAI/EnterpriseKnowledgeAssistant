"use client";

import { useRef, useState } from "react";
import { Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/dialog";
import { ErrorNotice } from "@/components/ui/feedback";
import { useToast } from "@/components/ui/toast";
import { isAbortError } from "@/lib/api/errors";
import { replaceDocumentFile } from "@/lib/api/upload";
import { formatBytes } from "@/lib/format";
import { useInvalidateAfterUpload } from "@/lib/hooks/documents";

const ACCEPT = ".pdf,.docx,.txt,.md,.markdown";

/** Upload a new version of the file; the previous version stays searchable until the new one is indexed. */
export function ReplaceFileButton({ documentId, onReplaced }: { documentId: string; onReplaced?: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const controller = useRef<AbortController | null>(null);
  const toast = useToast();
  const refresh = useInvalidateAfterUpload();
  const [upload, setUpload] = useState<{ file: File; progress: number; error: unknown } | null>(null);

  async function start(file: File) {
    const abort = new AbortController();
    controller.current = abort;
    setUpload({ file, progress: 0, error: null });
    try {
      await replaceDocumentFile(documentId, file, {
        signal: abort.signal,
        onProgress: (progress) => setUpload((current) => (current ? { ...current, progress } : current)),
      });
      setUpload(null);
      toast.success("New version uploaded and queued for indexing");
      await refresh();
      onReplaced?.();
    } catch (error) {
      if (isAbortError(error)) setUpload(null);
      else setUpload({ file, progress: 0, error });
    } finally {
      controller.current = null;
    }
  }

  const uploading = upload !== null && upload.error === null;
  const percent = Math.round((upload?.progress ?? 0) * 100);
  return (
    <>
      <Button onClick={() => input.current?.click()} disabled={uploading}>
        <Upload aria-hidden className="size-4" /> Replace file
      </Button>
      <input
        ref={input}
        type="file"
        accept={ACCEPT}
        className="sr-only"
        tabIndex={-1}
        aria-label="Choose the replacement file"
        data-testid="replace-input"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (file) void start(file);
        }}
      />
      <Modal
        open={upload !== null}
        onOpenChange={(open) => {
          if (open) return;
          controller.current?.abort();
          setUpload(null);
        }}
        title="Uploading new version"
        description={upload ? `${upload.file.name} (${formatBytes(upload.file.size)})` : undefined}
        size="sm"
        footer={
          <Button
            onClick={() => {
              controller.current?.abort();
              setUpload(null);
            }}
          >
            {uploading ? "Cancel" : "Close"}
          </Button>
        }
      >
        {upload?.error ? (
          <ErrorNotice error={upload.error} title="Upload failed" />
        ) : (
          <div>
            <div
              role="progressbar"
              aria-label="Upload progress"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent}
              className="h-2 overflow-hidden rounded-full bg-slate-200"
            >
              <div className="h-full rounded-full bg-accent-600 transition-[width]" style={{ width: `${percent}%` }} />
            </div>
            <p className="mt-2 text-sm text-slate-600">
              {percent >= 100 ? "Processing on the server..." : `${percent}%`}
            </p>
          </div>
        )}
      </Modal>
    </>
  );
}
