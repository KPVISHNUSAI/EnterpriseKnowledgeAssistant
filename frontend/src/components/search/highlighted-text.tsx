import type { components } from "@/lib/api/schema";

type Segment = components["schemas"]["HighlightSegment"];

/**
 * Search snippet with the matching terms in <mark>. Segments are rendered as React text, so
 * markup inside a document ("<script>...") is shown literally and never interpreted as HTML.
 */
export function HighlightedText({ segments, fallback }: { segments: Segment[]; fallback: string }) {
  if (segments.length === 0) return <>{fallback}</>;
  return (
    <>
      {segments.map((segment, index) =>
        segment.match ? (
          <mark key={index} className="rounded-sm bg-amber-200/80 px-0.5 text-inherit">
            {segment.text}
          </mark>
        ) : (
          <span key={index}>{segment.text}</span>
        ),
      )}
    </>
  );
}
