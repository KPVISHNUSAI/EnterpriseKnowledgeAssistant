import { apiUrl, csrfHeaders } from "./client";
import { apiErrorFrom, networkError } from "./errors";
import type { DocumentUploadResult, DocumentVisibility } from "./types";

export interface UploadOptions {
  /** Fraction of the request body sent so far, 0..1. */
  onProgress?: (fraction: number) => void;
  signal?: AbortSignal;
}

/**
 * Multipart upload through XMLHttpRequest: fetch() cannot report upload progress.
 * Rejects with ApiError (parsed error envelope) or an AbortError.
 */
export function sendForm<T>(
  method: "POST" | "PUT",
  path: string,
  form: FormData,
  { onProgress, signal }: UploadOptions = {},
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Upload cancelled", "AbortError"));
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open(method, apiUrl(path));
    xhr.responseType = "text";
    xhr.setRequestHeader("Accept", "application/json");
    for (const [name, value] of Object.entries(csrfHeaders(method))) xhr.setRequestHeader(name, value);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress?.(event.loaded / event.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as T);
        } catch {
          reject(apiErrorFrom(xhr.status, "", (name) => xhr.getResponseHeader(name)));
        }
      } else {
        reject(apiErrorFrom(xhr.status, xhr.responseText, (name) => xhr.getResponseHeader(name)));
      }
    };
    xhr.onerror = () => reject(networkError());
    xhr.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(form);
  });
}

export interface NewDocument {
  file: File;
  workspaceId: string;
  title?: string;
  visibility: DocumentVisibility;
}

export function uploadDocument(doc: NewDocument, options?: UploadOptions) {
  const form = new FormData();
  form.append("file", doc.file);
  form.append("workspace_id", doc.workspaceId);
  if (doc.title) form.append("title", doc.title);
  form.append("visibility", doc.visibility);
  return sendForm<DocumentUploadResult>("POST", "/documents/upload", form, options);
}

export function replaceDocumentFile(documentId: string, file: File, options?: UploadOptions) {
  const form = new FormData();
  form.append("file", file);
  return sendForm<DocumentUploadResult>("PUT", `/documents/${encodeURIComponent(documentId)}/file`, form, options);
}
