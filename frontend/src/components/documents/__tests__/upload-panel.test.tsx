import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CSRF_COOKIE } from "@/lib/api/client";
import { makeWorkspace } from "@/test/fixtures";
import { FakeXhr } from "@/test/fake-xhr";
import { jsonResponse, renderWithProviders } from "@/test/render";
import { UploadPanel } from "../upload-panel";

const fetchMock = vi.fn();
const policies = makeWorkspace({ name: "Policies", can_upload: true });
const engineering = makeWorkspace({ name: "Engineering", can_upload: true, my_role: "editor" });
const readOnly = makeWorkspace({ name: "HR (read only)", can_upload: false, my_role: "viewer" });

beforeEach(() => {
  FakeXhr.instances = [];
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url: string) =>
    String(url).endsWith("/workspaces")
      ? jsonResponse([policies, engineering, readOnly])
      : jsonResponse({ items: [], total: 0, page: 1, page_size: 20 }),
  );
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  document.cookie = `${CSRF_COOKIE}=csrf-up; path=/`;
});
afterEach(() => vi.unstubAllGlobals());

const pdf = (name = "report.pdf") => new File(["%PDF-1.4"], name, { type: "application/pdf" });
const pick = (user: ReturnType<typeof userEvent.setup>, ...files: File[]) =>
  user.upload(screen.getByTestId("upload-input"), files);
const xhrs = () => FakeXhr.instances;

async function renderPanel() {
  const user = userEvent.setup({ applyAccept: false });
  renderWithProviders(<UploadPanel />);
  await screen.findByTestId("upload-dropzone");
  return user;
}

describe("UploadPanel", () => {
  it("only offers workspaces the user can upload to", async () => {
    await renderPanel();
    const options = within(screen.getByTestId("upload-workspace")).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["Policies", "Engineering"]);
  });

  it("explains when there is nowhere to upload", async () => {
    fetchMock.mockImplementation(async () => jsonResponse([readOnly]));
    renderWithProviders(<UploadPanel />);
    expect(await screen.findByText(/do not have upload access to any workspace/)).toBeInTheDocument();
    expect(screen.queryByTestId("upload-input")).not.toBeInTheDocument();
  });

  it("uploads each file as multipart to the chosen workspace with the chosen visibility and shows real progress", async () => {
    const user = await renderPanel();
    await user.selectOptions(screen.getByTestId("upload-workspace"), "Engineering");
    await user.click(
      within(screen.getByTestId("upload-visibility")).getByRole("radio", { name: "Only people I share with" }),
    );
    await pick(user, pdf());

    await waitFor(() => expect(xhrs()).toHaveLength(1));
    const xhr = xhrs()[0]!;
    const form = xhr.body as FormData;
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("/api/v1/documents/upload");
    expect(xhr.headers["X-CSRF-Token"]).toBe("csrf-up");
    expect(form.get("workspace_id")).toBe(engineering.id);
    expect(form.get("visibility")).toBe("restricted");
    expect((form.get("file") as File).name).toBe("report.pdf");

    act(() => xhr.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 }));
    expect(screen.getByRole("progressbar", { name: "Uploading report.pdf" })).toHaveAttribute("aria-valuenow", "50");
    expect(screen.getByText("Uploading 50%")).toBeInTheDocument();

    act(() => xhr.upload.onprogress?.({ lengthComputable: true, loaded: 100, total: 100 }));
    expect(screen.getByText("Processing on the server...")).toBeInTheDocument();

    act(() => xhr.respond(201, { document: { id: "doc-1" }, job: {} }));
    expect(await screen.findByText(/Uploaded and queued for indexing/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open" })).toHaveAttribute("href", "/documents/doc-1");
    expect(screen.getByTestId("upload-item")).toHaveAttribute("data-status", "done");
  });

  it("rejects unsupported and empty files without contacting the server", async () => {
    const user = await renderPanel();
    await pick(user, new File(["MZ"], "setup.exe"), new File([], "empty.txt"));
    expect(await screen.findByText("Unsupported file type. Use PDF, DOCX, TXT or Markdown.")).toBeInTheDocument();
    expect(screen.getByText("This file is empty.")).toBeInTheDocument();
    expect(xhrs()).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /Retry/ })).not.toBeInTheDocument();
  });

  it("uploads at most two files at a time and starts the next as one finishes", async () => {
    const user = await renderPanel();
    await pick(user, pdf("a.pdf"), pdf("b.pdf"), pdf("c.pdf"));
    await waitFor(() => expect(xhrs()).toHaveLength(2));
    expect(screen.getAllByTestId("upload-item").map((i) => i.getAttribute("data-status"))).toEqual([
      "uploading",
      "uploading",
      "waiting",
    ]);

    act(() => xhrs()[0]!.respond(201, { document: { id: "d-a" }, job: {} }));
    await waitFor(() => expect(xhrs()).toHaveLength(3));
  });

  it("points a duplicate upload (409) at the existing document", async () => {
    const user = await renderPanel();
    await pick(user, pdf());
    await waitFor(() => expect(xhrs()).toHaveLength(1));
    act(() =>
      xhrs()[0]!.respond(409, {
        error: {
          code: "conflict",
          message: "Duplicate",
          details: { document_id: "existing-1" },
          request_id: "req-dup",
        },
      }),
    );
    expect(await screen.findByText("This file already exists in the workspace.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View existing document" })).toHaveAttribute(
      "href",
      "/documents/existing-1",
    );
    expect(screen.queryByRole("button", { name: /Retry/ })).not.toBeInTheDocument();
  });

  it("shows the server error with its request id and lets the user retry", async () => {
    const user = await renderPanel();
    await pick(user, pdf());
    await waitFor(() => expect(xhrs()).toHaveLength(1));
    act(() =>
      xhrs()[0]!.respond(415, {
        error: {
          code: "unsupported_media_type",
          message: "File content does not match its type",
          request_id: "req-415",
        },
      }),
    );
    expect(await screen.findByText(/File content does not match its type/)).toBeInTheDocument();
    expect(screen.getByText(/req-415/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry report.pdf" }));
    await waitFor(() => expect(xhrs()).toHaveLength(2));
    expect(screen.getByTestId("upload-item")).toHaveAttribute("data-status", "uploading");
  });

  it("can cancel an upload in flight", async () => {
    const user = await renderPanel();
    await pick(user, pdf());
    await waitFor(() => expect(xhrs()).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(xhrs()[0]!.aborted).toBe(true);
    expect(await screen.findByText("Upload cancelled.")).toBeInTheDocument();
  });
});
