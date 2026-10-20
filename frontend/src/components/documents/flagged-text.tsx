"use client";

import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { Alert, Badge } from "@/components/ui/feedback";
import type { ChunkFlag, DocumentDetail, DocumentItem } from "@/lib/api/types";
import { FLAG_RULE_LABELS } from "@/lib/labels";
import { useSetFlaggedText } from "@/lib/hooks/documents";

const passages = (count: number) => (count === 1 ? "1 passage" : `${count} passages`);

function pagesText(pages: number[]) {
  if (pages.length === 0) return "";
  if (pages.length === 1) return ` on page ${pages[0]}`;
  return ` on pages ${pages.slice(0, -1).join(", ")} and ${pages[pages.length - 1]}`;
}

/** Amber while text addressed to AI assistants is kept out of answers, red once a manager let it in. */
export function FlaggedTextBadge({ doc }: { doc: DocumentItem }) {
  if (!doc.flagged_chunks) return null;
  return doc.flagged_text_included ? (
    <Badge
      tone="danger"
      data-testid="flagged-badge"
      title="Text that tries to instruct AI assistants was found, and a document manager let it into answers"
    >
      AI instructions included
    </Badge>
  ) : (
    <Badge
      tone="warning"
      data-testid="flagged-badge"
      title="Text that tries to instruct AI assistants was found and is left out of answers"
    >
      AI instructions removed
    </Badge>
  );
}

export function FlaggedTextNotice({ doc }: { doc: DocumentDetail }) {
  const confirm = useConfirm();
  const setIncluded = useSetFlaggedText(doc.id);
  if (!doc.flagged_chunks) return null;
  const where = `${passages(doc.flagged_chunks)}${pagesText(doc.flagged_pages ?? [])}`;

  async function include() {
    const ok = await confirm({
      title: "Include the flagged text in answers?",
      description:
        "The assistant will read this text again when it answers questions about this document. Do this only if " +
        "you have read the highlighted text and it is the document's own content, not instructions planted for AI " +
        "assistants. Indexing the document again removes it from answers until someone includes it again.",
      confirmLabel: "Include in answers",
      destructive: true,
    });
    if (ok) setIncluded.mutate({ included: true, version: doc.version });
  }

  if (doc.flagged_text_included) {
    return (
      <Alert tone="danger" title="Flagged text is included in answers" data-testid="flagged-notice">
        <p>
          Text that tries to instruct AI assistants was found in {where}, and a document manager let the assistant read
          it. Links in answers still only work when the cited passage contains them.
        </p>
        {doc.can_manage ? (
          <Button
            size="sm"
            variant="secondary"
            className="mt-2"
            loading={setIncluded.isPending}
            onClick={() => setIncluded.mutate({ included: false, version: doc.version })}
            data-testid="flagged-exclude"
          >
            Remove from answers
          </Button>
        ) : null}
      </Alert>
    );
  }
  return (
    <Alert tone="warning" title="Instructions addressed to AI assistants found" data-testid="flagged-notice">
      <p>
        Found in {where}. This text is left out of what the assistant reads, so answers cannot follow it. It is
        highlighted in the extracted text.
      </p>
      {doc.can_manage ? (
        <Button
          size="sm"
          variant="secondary"
          className="mt-2"
          loading={setIncluded.isPending}
          onClick={include}
          data-testid="flagged-include"
        >
          Include in answers
        </Button>
      ) : null}
    </Alert>
  );
}

/**
 * The chunk's text with its flagged ranges marked. Ranges count Unicode code points (the API's
 * Python strings), so the text is split into code points, not UTF-16 units.
 */
export function FlaggedContent({ content, flags }: { content: string; flags: ChunkFlag[] }) {
  if (flags.length === 0) return <>{content}</>;
  const chars = Array.from(content);
  const parts = [];
  let position = 0;
  for (const flag of [...flags].sort((a, b) => a.start - b.start || b.end - a.end)) {
    const start = Math.max(flag.start, position);
    if (flag.end <= start) continue;
    parts.push(chars.slice(position, start).join(""));
    parts.push(
      <mark
        key={`${flag.start}-${flag.end}`}
        className="rounded-sm bg-amber-200/70 text-amber-950 ring-1 ring-amber-400"
        title={FLAG_RULE_LABELS[flag.rule]}
        data-testid="flagged-text"
      >
        {chars.slice(start, flag.end).join("")}
      </mark>,
    );
    position = flag.end;
  }
  parts.push(chars.slice(position).join(""));
  return <>{parts}</>;
}
