import type { Parent, PhrasingContent, Root, RootContent } from "mdast";

/** `[S1]`, `[S1, S2]` or `[S1;S2]`: the source labels the model is told to cite. */
const MARKER = /\[(S\d{1,3}(?:\s*[,;]\s*S\d{1,3})*)\]/g;
const HREF_PREFIX = "#cite-";

export type CitationPart = { type: "text"; text: string } | { type: "cite"; id: string };

/** Split text into plain runs and single citations ("[S1, S2]" yields two). */
export function splitCitations(text: string): CitationPart[] {
  const parts: CitationPart[] = [];
  let last = 0;
  for (const match of text.matchAll(MARKER)) {
    if (match.index > last) parts.push({ type: "text", text: text.slice(last, match.index) });
    for (const id of match[1].split(/[,;]/)) parts.push({ type: "cite", id: id.trim() });
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push({ type: "text", text: text.slice(last) });
  return parts;
}

/** Unique source labels cited in the text, in order of first appearance. */
export function citedSourceIds(text: string): string[] {
  const ids = splitCitations(text).flatMap((part) => (part.type === "cite" ? [part.id] : []));
  return [...new Set(ids)];
}

export const citationHref = (id: string) => `${HREF_PREFIX}${id}`;

export function citationIdFromHref(href: string | undefined): string | null {
  return href?.match(/^#cite-(S\d{1,3})$/)?.[1] ?? null;
}

// Markdown nodes whose text must stay literal (code) or cannot hold a nested link.
const LITERAL = new Set([
  "code",
  "inlineCode",
  "link",
  "linkReference",
  "definition",
  "html",
  "image",
  "imageReference",
]);

function rewrite(parent: Parent): void {
  const children: RootContent[] = [];
  for (const node of parent.children) {
    if (node.type === "text") {
      for (const part of splitCitations(node.value)) {
        children.push(
          part.type === "text"
            ? { type: "text", value: part.text }
            : { type: "link", url: citationHref(part.id), children: [{ type: "text", value: part.id }] },
        );
      }
      continue;
    }
    if ("children" in node && !LITERAL.has(node.type)) rewrite(node as Parent);
    children.push(node);
  }
  parent.children = children as PhrasingContent[];
}

/** remark plugin: turn citation markers into links to `#cite-S1`, rendered as chips. */
export function remarkCitations() {
  return (tree: Root) => rewrite(tree);
}
