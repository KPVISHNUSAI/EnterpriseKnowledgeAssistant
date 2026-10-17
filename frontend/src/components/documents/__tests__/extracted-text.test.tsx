import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Chunk } from "@/lib/api/types";
import { jsonResponse, renderWithProviders } from "@/test/render";
import { ExtractedText } from "../extracted-text";

const fetchMock = vi.fn();
const TOTAL = 250;
const chunk = (i: number): Chunk => ({
  id: `chunk-${i}`,
  chunk_index: i,
  content: `Text of chunk ${i} <b>literal</b>`,
  page_number: Math.floor(i / 10) + 1,
  page_end: null,
  section_title: i % 10 === 0 ? `Section ${Math.floor(i / 10) + 1}` : null,
  heading_path: null,
  token_count: 300,
});

function servePages() {
  fetchMock.mockImplementation(async (url: string) => {
    const params = new URL(url, "http://x").searchParams;
    const page = Number(params.get("page"));
    const size = Number(params.get("page_size"));
    const items = Array.from({ length: TOTAL }, (_, i) => chunk(i)).slice((page - 1) * size, page * size);
    return jsonResponse({ items, total: TOTAL, page, page_size: size });
  });
}
const requestedPages = () =>
  fetchMock.mock.calls.map(([url]) => new URL(String(url), "http://x").searchParams.get("page"));

beforeEach(() => {
  fetchMock.mockReset();
  servePages();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.restoreAllMocks());

describe("ExtractedText", () => {
  it("shows chunks grouped by page and renders document text literally", async () => {
    renderWithProviders(
      <ExtractedText documentId="doc-1" status="indexed" chunkCount={TOTAL} indexedAt="2026-01-01T00:00:00Z" />,
    );
    expect(await screen.findAllByTestId("chunk")).toHaveLength(100);
    expect(screen.getByRole("heading", { name: "Page 1" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Page 2" })).toBeInTheDocument();
    expect(screen.getByText("Section 1")).toBeInTheDocument();
    expect(screen.getByText("Text of chunk 3 <b>literal</b>")).toBeInTheDocument(); // not parsed as HTML
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
  });

  it("loads further pages until the cited chunk is found, then highlights and scrolls to it", async () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    renderWithProviders(
      <ExtractedText documentId="doc-1" status="indexed" chunkCount={TOTAL} indexedAt={null} chunkId="chunk-220" />,
    );

    const cited = await screen.findByLabelText("Cited passage, chunk 221");
    expect(cited).toHaveAttribute("data-highlighted", "true");
    expect(cited).toHaveTextContent("Cited passage");
    expect(requestedPages()).toEqual(["1", "2", "3"]);
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("scrolls to the first chunk of a page without highlighting anything", async () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    renderWithProviders(
      <ExtractedText documentId="doc-1" status="indexed" chunkCount={TOTAL} indexedAt={null} page={12} />,
    );
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(1));
    expect(requestedPages()).toEqual(["1", "2"]);
    expect(document.querySelector("[data-highlighted]")).toBeNull();
  });

  it("warns when the cited chunk no longer exists in this version", async () => {
    renderWithProviders(
      <ExtractedText
        documentId="doc-1"
        status="indexed"
        chunkCount={TOTAL}
        indexedAt={null}
        chunkId="chunk-from-an-older-version"
      />,
    );
    expect(await screen.findByText(/not part of the current version of this document/)).toBeInTheDocument();
    expect(requestedPages()).toEqual(["1", "2", "3"]);
  });

  it("offers a way to jump to the PDF page", async () => {
    const onShowInPdf = vi.fn();
    renderWithProviders(
      <ExtractedText
        documentId="doc-1"
        status="indexed"
        chunkCount={TOTAL}
        indexedAt={null}
        onShowInPdf={onShowInPdf}
      />,
    );
    const [first] = await screen.findAllByRole("button", { name: "Show in PDF" });
    first!.click();
    expect(onShowInPdf).toHaveBeenCalledWith(1);
  });

  it("does not request text for a document that has never been indexed", () => {
    const { rerender } = renderWithProviders(
      <ExtractedText documentId="doc-1" status="queued" chunkCount={0} indexedAt={null} />,
    );
    expect(screen.getByText(/appear here once indexing has finished/)).toBeInTheDocument();
    rerender(<ExtractedText documentId="doc-1" status="failed" chunkCount={0} indexedAt={null} />);
    expect(screen.getByText(/indexing failed/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps showing the previous version's text while a replacement file is indexed", async () => {
    renderWithProviders(
      <ExtractedText documentId="doc-1" status="processing" chunkCount={TOTAL} indexedAt="2026-01-01T00:00:00Z" />,
    );
    expect(await screen.findAllByTestId("chunk")).toHaveLength(100);
    expect(screen.getByText(/still being indexed. This is the text of the previous version/)).toBeInTheDocument();
  });

  it("fetches the new text once indexing finishes (a new indexed_at is a new query)", async () => {
    const { rerender } = renderWithProviders(
      <ExtractedText documentId="doc-1" status="processing" chunkCount={TOTAL} indexedAt="2026-01-01T00:00:00Z" />,
    );
    await screen.findAllByTestId("chunk");
    const before = fetchMock.mock.calls.length;
    rerender(<ExtractedText documentId="doc-1" status="indexed" chunkCount={TOTAL} indexedAt="2026-01-02T00:00:00Z" />);
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before));
    expect(screen.queryByText(/previous version/)).not.toBeInTheDocument();
  });
});
