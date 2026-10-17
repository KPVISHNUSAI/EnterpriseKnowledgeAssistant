"use client";

import { formatDateTime, formatRelative } from "@/lib/format";
import { useNow } from "@/lib/hooks/now";

/** A timestamp as "5 minutes ago" (full date in the tooltip), kept fresh while on screen. */
export function Time({ iso, absolute }: { iso: string | null | undefined; absolute?: boolean }) {
  const now = useNow();
  if (!iso) return <span>-</span>;
  const full = formatDateTime(iso);
  return (
    <time dateTime={iso} title={full}>
      {absolute || now === 0 ? full : formatRelative(iso, now)}
    </time>
  );
}
