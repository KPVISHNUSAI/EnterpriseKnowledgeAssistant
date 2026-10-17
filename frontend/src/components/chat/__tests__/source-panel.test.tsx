import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { makeSource } from "@/test/fixtures";
import { SourcePanel } from "../source-panel";

describe("SourcePanel", () => {
  it("lists the passages with title, page, section, excerpt, relevance and cited state", () => {
    const sources = [
      makeSource("S1"),
      makeSource("S2", { cited: false, page_number: 7, section_title: null, relevance: { rrf_score: 0.0312 } }),
    ];
    render(<SourcePanel sources={sources} selectedId={null} onClose={() => {}} />);

    expect(screen.getByTestId("source-panel")).toHaveTextContent("2 passages used · 1 cited");
    const [first, second] = screen.getAllByTestId("source-item");
    expect(first).toHaveTextContent("Employee handbook");
    expect(first).toHaveTextContent("Page 4 · Leave");
    expect(first).toHaveTextContent("Employees accrue 1.5 days of paid leave per month.");
    expect(first).toHaveTextContent("Rerank 0.910");
    expect(within(first!).getByText("Cited")).toBeInTheDocument();
    expect(within(second!).getByText("Not cited")).toBeInTheDocument();
    expect(second).toHaveTextContent("RRF 0.031");
  });

  it("links to the document at the cited page and chunk", () => {
    const source = makeSource("S1", { page_number: 4 });
    render(<SourcePanel sources={[source]} selectedId={null} onClose={() => {}} />);
    const href = screen.getAllByRole("link", { name: /Employee handbook|Open document/ })[0]!.getAttribute("href");
    expect(href).toBe(`/documents/${source.document_id}?page=4&chunk=${source.chunk_id}`);
  });

  it("shows a redacted passage without its text or a link", () => {
    const source = makeSource("S3", { excerpt: null, redacted: true, document_title: "Secret salary bands" });
    render(<SourcePanel sources={[source]} selectedId={null} onClose={() => {}} />);
    const item = screen.getByTestId("source-item");
    expect(item).toHaveAttribute("data-redacted", "true");
    expect(item).toHaveTextContent("Restricted source");
    expect(item).toHaveTextContent("Your access to this document has changed");
    expect(item).not.toHaveTextContent("Secret salary bands");
    expect(within(item).queryByRole("link")).not.toBeInTheDocument();
  });

  it("highlights the passage a citation chip pointed at and scrolls to it", () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    render(<SourcePanel sources={[makeSource("S1"), makeSource("S2")]} selectedId="S2" onClose={() => {}} />);
    const [first, second] = screen.getAllByTestId("source-item");
    expect(second).toHaveClass("ring-2");
    expect(first).not.toHaveClass("ring-2");
    expect(scroll).toHaveBeenCalledTimes(1);
    scroll.mockRestore();
  });

  it("has an empty state and a close button", async () => {
    const onClose = vi.fn();
    render(<SourcePanel sources={[]} selectedId={null} onClose={onClose} />);
    expect(screen.getByText("No passages were retrieved for this answer.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Close sources" }));
    expect(onClose).toHaveBeenCalled();
  });
});
