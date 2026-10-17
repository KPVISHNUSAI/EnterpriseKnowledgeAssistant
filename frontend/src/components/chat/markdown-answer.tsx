"use client";

import { isValidElement, memo, useMemo, type ComponentProps, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import { CopyButton } from "@/components/ui/copy-button";
import { cn } from "@/lib/cn";
import type { Citation, SourceRef } from "@/lib/api/types";
import { citationIdFromHref } from "@/lib/citations";
import { rehypePlugins, remarkPlugins } from "@/lib/markdown";
import { CitationChip } from "./citation-chip";

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement(node)) return textOf((node.props as { children?: ReactNode }).children);
  return "";
}

function CodeBlock({ children }: ComponentProps<"pre">) {
  const code = isValidElement(children) ? (children.props as { className?: string }) : {};
  const language = /language-([\w+-]+)/.exec(code.className ?? "")?.[1];
  return (
    <div className="my-3 overflow-hidden rounded-lg bg-slate-900 text-slate-100" data-testid="code-block">
      <div className="flex items-center justify-between border-b border-white/10 px-3 py-1">
        <span className="font-mono text-xs text-slate-400">{language ?? "text"}</span>
        <CopyButton
          text={() => textOf(children).replace(/\n$/, "")}
          label="Copy code"
          className="h-6 text-xs text-slate-300 hover:bg-white/10 hover:text-white"
        />
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-[13px] leading-relaxed">{children}</pre>
    </div>
  );
}

/** Where a `[S1]` marker points, and whether it can be trusted. */
function describeCitation(id: string, sources: SourceRef[], citations: Citation[]) {
  const source = sources.find((s) => s.source_id === id);
  const citation = citations.find((c) => c.source_id === id);
  const verified = Boolean(source) && (citation ? citation.verified : true);
  // A passage the reader can no longer open must not leak its title through the tooltip.
  if (source?.redacted || citation?.redacted) return { verified, label: "Restricted source" };
  const title = source?.document_title ?? citation?.document_title ?? undefined;
  const page = source?.page_number ?? citation?.page_number;
  return { verified, label: title ? `${title}${page ? `, page ${page}` : ""}` : undefined };
}

interface Props {
  text: string;
  sources?: SourceRef[];
  citations?: Citation[];
  onCite?: (sourceId: string) => void;
  /** Show a blinking cursor after the last block while the answer is still being written. */
  streaming?: boolean;
}

/**
 * Model output as sanitized Markdown (GFM tables, highlighted code with copy) with `[S1]`
 * markers rendered as citation chips. Never uses dangerouslySetInnerHTML.
 */
export const MarkdownAnswer = memo(function MarkdownAnswer({
  text,
  sources = [],
  citations = [],
  onCite,
  streaming,
}: Props) {
  const components = useMemo<Components>(
    () => ({
      a({ href, children }) {
        const id = citationIdFromHref(href);
        if (id) {
          const { verified, label } = describeCitation(id, sources, citations);
          return (
            <CitationChip
              id={id}
              state={verified ? "verified" : "unverified"}
              label={label}
              onClick={() => onCite?.(id)}
            />
          );
        }
        const external = typeof href === "string" && !href.startsWith("#") && !href.startsWith("/");
        return (
          <a href={href} {...(external ? { target: "_blank", rel: "noopener noreferrer nofollow" } : {})}>
            {children}
          </a>
        );
      },
      pre: CodeBlock,
      table: ({ children }) => (
        <div className="overflow-x-auto">
          <table>{children}</table>
        </div>
      ),
    }),
    [sources, citations, onCite],
  );

  return (
    <div className={cn("markdown text-[15px] text-slate-800", streaming && "streaming")}>
      <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
});
