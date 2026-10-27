import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Claim, DocumentChange, SourceRef } from "@/lib/api/types";
import { fromMessage, type DisplayMessage } from "@/lib/chat-state";
import { formatDate } from "@/lib/format";
import { AnswerReceipt, ReplacedSources, claimReason, receiptSummary } from "../answer-receipt";
import { MessageView } from "../message-view";

const claim = (overrides: Partial<Claim>): Claim => ({
  text: "Employees receive 25 days of paid vacation per year.",
  source_ids: ["S1"],
  verdict: "supported",
  missing_numbers: [],
  ...overrides,
});

const source = (overrides: Partial<SourceRef>): SourceRef => ({
  source_id: "S1",
  chunk_id: "c1",
  document_id: "d1",
  document_title: "Employee Handbook",
  workspace_id: "w1",
  document_version: 1,
  excerpt: "Employees receive 25 days of paid vacation per year.",
  relevance: {},
  cited: true,
  redacted: false,
  ...overrides,
});

const change: DocumentChange = {
  document_id: "d1",
  document_title: "Employee Handbook",
  used_version: 1,
  current_version: 2,
  current_since: "2026-10-26T09:30:00Z",
};

describe("claimReason", () => {
  it.each([
    [claim({}), "Matches S1"],
    [claim({ source_ids: ["S1", "S3"] }), "Matches S1, S3"],
    [claim({ verdict: "unsupported", missing_numbers: ["3"] }), "“3” is not in S1"],
    [claim({ verdict: "unsupported", missing_numbers: ["3", "14"] }), "“3”, “14” are not in S1"],
    [claim({ verdict: "unsupported" }), "Too few of its words are in S1"],
    [claim({ verdict: "uncited", source_ids: [] }), "Cites no source"],
  ])("explains the verdict", (checked, reason) => {
    expect(claimReason(checked)).toBe(reason);
  });
});

describe("receiptSummary", () => {
  it("counts the statements that match", () => {
    expect(receiptSummary([claim({}), claim({ verdict: "uncited", source_ids: [] })])).toBe(
      "1 of 2 statements match their sources",
    );
    expect(receiptSummary([claim({})])).toBe("1 of 1 statement matches its source");
  });
});

describe("AnswerReceipt", () => {
  it("lists every statement with its reason and the versions the answer used", () => {
    render(
      <AnswerReceipt
        claims={[
          claim({}),
          claim({ text: "Approval needs 3 directors.", verdict: "unsupported", missing_numbers: ["3"] }),
        ]}
        sources={[
          source({}),
          source({ source_id: "S2", chunk_id: "c2" }), // same document: named once
          source({ source_id: "S3", document_id: "d2", document_title: "Notes", cited: false }),
        ]}
      />,
    );
    expect(screen.getByTestId("receipt-summary")).toHaveTextContent("1 of 2 statements match their sources");
    const items = screen.getAllByTestId("receipt-claim");
    expect(items.map((item) => item.dataset.verdict)).toEqual(["supported", "unsupported"]);
    expect(within(items[1]).getByText("“3” is not in S1")).toBeInTheDocument();
    // Only cited documents: an uncited passage did not shape the answer.
    expect(screen.getByTestId("receipt-documents")).toHaveTextContent(/^Based on Employee Handbook \(version 1\)$/);
    expect(screen.getByText(/not proof/)).toBeInTheDocument();
  });

  it("names a document without a recorded version by title only", () => {
    render(<AnswerReceipt claims={[claim({})]} sources={[source({ document_version: null })]} />);
    expect(screen.getByTestId("receipt-documents")).toHaveTextContent(/^Based on Employee Handbook$/);
  });
});

describe("ReplacedSources", () => {
  it("says which version the answer used and when it was replaced", () => {
    render(<ReplacedSources changes={[change]} />);
    const notice = screen.getByTestId("replaced-sources");
    expect(notice).toHaveTextContent("A source has a newer version");
    expect(notice).toHaveTextContent(
      `This answer used version 1 of “Employee Handbook”. Version 2 replaced it on ${formatDate(change.current_since)}.`,
    );
    expect(notice).toHaveTextContent("Ask again for an answer from the current version.");
  });
});

describe("fromMessage", () => {
  it("keeps the receipt and the replaced versions of a stored answer", () => {
    const stored = fromMessage({
      id: "m1",
      conversation_id: "c1",
      role: "assistant",
      content: "text",
      status: "complete",
      claims: [claim({})],
      changed_documents: [change],
      redacted: false,
      created_at: "2026-10-27T00:00:00Z",
    });
    expect(stored.claims).toEqual([claim({})]);
    expect(stored.changedDocuments).toEqual([change]);
  });
});

describe("MessageView", () => {
  const answer = (overrides: Partial<DisplayMessage>): DisplayMessage => ({
    id: "m1",
    role: "assistant",
    content: "Employees receive 25 days of paid vacation per year [S1].",
    status: "complete",
    answerStatus: "answered",
    sources: [source({})],
    citations: [],
    claims: [claim({})],
    changedDocuments: [change],
    ...overrides,
  });
  const view = (message: DisplayMessage) =>
    render(
      <MessageView message={message} canRetry={false} onCite={vi.fn()} onOpenSources={vi.fn()} onRetry={vi.fn()} />,
    );

  it("shows the receipt and the replaced-version notice under a finished answer", () => {
    view(answer({}));
    expect(screen.getByTestId("answer-receipt")).toBeInTheDocument();
    expect(screen.getByTestId("replaced-sources")).toBeInTheDocument();
  });

  it.each([
    ["while it streams", { streaming: true }],
    ["once the answer is withheld", { redacted: true, content: "" }],
  ])("shows neither %s", (_, overrides) => {
    view(answer(overrides));
    expect(screen.queryByTestId("answer-receipt")).not.toBeInTheDocument();
    expect(screen.queryByTestId("replaced-sources")).not.toBeInTheDocument();
  });
});
