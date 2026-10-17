import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CSRF_COOKIE } from "../api/client";
import { ApiError } from "../api/errors";
import { replaceDocumentFile, sendForm, uploadDocument } from "../api/upload";
import { FakeXhr } from "@/test/fake-xhr";

const last = () => FakeXhr.instances[FakeXhr.instances.length - 1]!;

beforeEach(() => {
  FakeXhr.instances = [];
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
});
afterEach(() => vi.unstubAllGlobals());

describe("sendForm (XMLHttpRequest upload)", () => {
  it("adds the CSRF header, posts to the API and resolves the parsed JSON", async () => {
    document.cookie = `${CSRF_COOKIE}=csrf-xyz; path=/`;
    const form = new FormData();
    const promise = sendForm("POST", "/documents/upload", form);
    const xhr = last();
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("/api/v1/documents/upload");
    expect(xhr.headers).toMatchObject({ "X-CSRF-Token": "csrf-xyz", Accept: "application/json" });
    expect(xhr.body).toBe(form);
    xhr.respond(201, { document: { id: "d1" } });
    await expect(promise).resolves.toEqual({ document: { id: "d1" } });
  });

  it("reports real upload progress as a fraction", async () => {
    const onProgress = vi.fn();
    const promise = sendForm("POST", "/documents/upload", new FormData(), { onProgress });
    const xhr = last();
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 25, total: 100 });
    xhr.upload.onprogress?.({ lengthComputable: false, loaded: 50, total: 0 });
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 100, total: 100 });
    expect(onProgress.mock.calls).toEqual([[0.25], [1]]);
    xhr.respond(201, {});
    await promise;
  });

  it("rejects with the parsed error envelope (including duplicate details)", async () => {
    const promise = sendForm("POST", "/documents/upload", new FormData());
    last().respond(
      409,
      { error: { code: "conflict", message: "Duplicate", details: { document_id: "doc-9" }, request_id: "req-7" } },
      {},
    );
    const err = await promise.catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: "conflict", requestId: "req-7", details: { document_id: "doc-9" } });
  });

  it("keeps Retry-After from rate-limit responses", async () => {
    const promise = sendForm("POST", "/documents/upload", new FormData());
    last().respond(429, { error: { code: "rate_limited", message: "Slow down" } }, { "retry-after": "12" });
    await expect(promise).rejects.toMatchObject({ status: 429, retryAfter: 12 });
  });

  it("maps a connection failure to a network error", async () => {
    const promise = sendForm("PUT", "/documents/x/file", new FormData());
    last().onerror?.();
    await expect(promise).rejects.toMatchObject({ code: "network_error" });
  });

  it("aborts the request when the signal fires, and refuses to start when already aborted", async () => {
    const abort = new AbortController();
    const promise = sendForm("POST", "/documents/upload", new FormData(), { signal: abort.signal });
    abort.abort();
    expect(last().aborted).toBe(true);
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });

    const done = new AbortController();
    done.abort();
    await expect(sendForm("POST", "/x", new FormData(), { signal: done.signal })).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(FakeXhr.instances).toHaveLength(1);
  });
});

describe("document upload helpers", () => {
  it("sends file, workspace, visibility and an optional title as multipart fields", async () => {
    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    const promise = uploadDocument({ file, workspaceId: "ws-1", visibility: "restricted", title: "My notes" });
    const form = last().body as FormData;
    expect((form.get("file") as File).name).toBe("notes.txt");
    expect(form.get("workspace_id")).toBe("ws-1");
    expect(form.get("visibility")).toBe("restricted");
    expect(form.get("title")).toBe("My notes");
    last().respond(201, {});
    await promise;
  });

  it("omits an empty title and replaces files with PUT /documents/{id}/file", async () => {
    const file = new File(["x"], "a.pdf");
    const first = uploadDocument({ file, workspaceId: "ws-1", visibility: "workspace" });
    expect((last().body as FormData).has("title")).toBe(false);
    last().respond(201, {});
    await first;

    const second = replaceDocumentFile("doc/1", file);
    expect(last().method).toBe("PUT");
    expect(last().url).toBe("/api/v1/documents/doc%2F1/file");
    last().respond(202, {});
    await second;
  });
});
