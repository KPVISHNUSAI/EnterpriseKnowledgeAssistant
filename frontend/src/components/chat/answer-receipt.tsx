import { ChevronRight, CircleAlert, CircleCheck, CircleHelp, ReceiptText } from "lucide-react";
import { Alert } from "@/components/ui/feedback";
import type { Claim, DocumentChange, SourceRef } from "@/lib/api/types";
import { formatDate } from "@/lib/format";

const quoted = (items: string[]) => items.map((item) => `“${item}”`).join(", ");

/** Why a statement got its verdict, in the words of the check: it compares words and numbers. */
export function claimReason(claim: Claim): string {
  const sources = claim.source_ids.join(", ");
  if (claim.verdict === "supported") return `Matches ${sources}`;
  if (claim.verdict === "uncited") return "Cites no source";
  const missing = claim.missing_numbers ?? [];
  if (missing.length) return `${quoted(missing)} ${missing.length === 1 ? "is" : "are"} not in ${sources}`;
  return `Too few of its words are in ${sources}`;
}

export function receiptSummary(claims: Claim[]): string {
  const matched = claims.filter((claim) => claim.verdict === "supported").length;
  const noun = claims.length === 1 ? "statement matches its source" : "statements match their sources";
  return `${matched} of ${claims.length} ${noun}`;
}

const VERDICT_ICON = {
  supported: <CircleCheck aria-hidden className="size-4 text-emerald-600" />,
  unsupported: <CircleAlert aria-hidden className="size-4 text-amber-600" />,
  uncited: <CircleHelp aria-hidden className="size-4 text-slate-500" />,
} as const;

/** Each statement of an answer checked against the passages it cites, and the versions it used. */
export function AnswerReceipt({ claims, sources }: { claims: Claim[]; sources: SourceRef[] }) {
  const allMatch = claims.every((claim) => claim.verdict === "supported");
  const cited = [...new Map(sources.filter((s) => s.cited).map((s) => [s.document_id, s])).values()];
  return (
    <details className="group mt-3 rounded-lg border border-slate-200 text-sm" data-testid="answer-receipt">
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-3 py-2 text-slate-700 hover:bg-slate-50 [&::-webkit-details-marker]:hidden">
        <ChevronRight
          aria-hidden
          className="size-4 shrink-0 text-slate-400 transition-transform group-open:rotate-90"
        />
        <ReceiptText aria-hidden className="size-4 shrink-0 text-slate-500" />
        <span>Receipt:</span>
        <span className={allMatch ? "text-slate-700" : "font-medium text-amber-700"} data-testid="receipt-summary">
          {receiptSummary(claims)}
        </span>
      </summary>
      <div className="space-y-3 border-t border-slate-200 px-3 py-3">
        <ol className="space-y-2">
          {claims.map((claim, index) => (
            <li key={index} className="flex gap-2" data-testid="receipt-claim" data-verdict={claim.verdict}>
              <span className="mt-0.5 shrink-0">{VERDICT_ICON[claim.verdict]}</span>
              <div className="min-w-0">
                <p className="break-words text-slate-800">{claim.text}</p>
                <p className="text-xs text-slate-500">{claimReason(claim)}</p>
              </div>
            </li>
          ))}
        </ol>
        {cited.length ? (
          <p className="text-xs break-words text-slate-600" data-testid="receipt-documents">
            Based on{" "}
            {cited
              .map((s) =>
                s.document_version != null ? `${s.document_title} (version ${s.document_version})` : s.document_title,
              )
              .join("; ")}
          </p>
        ) : null}
        <p className="text-xs text-slate-500">
          Checked by matching each statement’s words and numbers with the passages it cites. A match is evidence that
          the statement says what the passage says, not proof.
        </p>
      </div>
    </details>
  );
}

/** Cited documents that have a newer indexed version than the one this answer used. */
export function ReplacedSources({ changes, className }: { changes: DocumentChange[]; className?: string }) {
  return (
    <Alert
      tone="warning"
      title={changes.length === 1 ? "A source has a newer version" : "Sources have newer versions"}
      className={className}
      data-testid="replaced-sources"
    >
      {changes.map((change) => (
        <p key={change.document_id} className="break-words">
          This answer used version {change.used_version} of “{change.document_title}”. Version {change.current_version}{" "}
          replaced it{change.current_since ? ` on ${formatDate(change.current_since)}` : ""}.
        </p>
      ))}
      <p className="mt-1">Ask again for an answer from the current version.</p>
    </Alert>
  );
}
