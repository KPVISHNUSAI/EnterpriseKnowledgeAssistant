import { cn } from "@/lib/cn";

export type CitationState = "verified" | "unverified";

/** An inline `[S1]` marker. Verified ones open the source panel; unverified ones only explain themselves. */
export function CitationChip({
  id,
  state,
  label,
  onClick,
}: {
  id: string;
  state: CitationState;
  /** Document title and page, used for the accessible name and tooltip. */
  label?: string;
  onClick?: () => void;
}) {
  const className = cn(
    "mx-0.5 inline-flex h-5 min-w-6 items-center justify-center rounded-md px-1.5 align-baseline text-[11px] leading-none font-semibold no-underline",
    state === "verified"
      ? "bg-accent-50 text-accent-800 ring-1 ring-accent-200 ring-inset hover:bg-accent-100"
      : "border border-dashed border-amber-500 bg-amber-50 text-amber-900",
  );
  if (state === "unverified") {
    return (
      <span
        data-testid="citation-chip"
        data-source-id={id}
        data-state="unverified"
        className={className}
        title="This citation could not be matched to a retrieved source"
        aria-label={`Unverified citation ${id}`}
      >
        {id}
      </span>
    );
  }
  return (
    <button
      type="button"
      data-testid="citation-chip"
      data-source-id={id}
      data-state="verified"
      className={className}
      title={label ?? `Source ${id}`}
      aria-label={label ? `Source ${id}: ${label}` : `Source ${id}`}
      onClick={onClick}
    >
      {id}
    </button>
  );
}
