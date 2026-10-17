import { AlertCircle, CheckCircle2, Clock, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/feedback";
import type { DocumentStatus } from "@/lib/api/types";
import { DOCUMENT_STATUS_LABELS } from "@/lib/labels";

const TONES = { queued: "neutral", processing: "info", indexed: "success", failed: "danger" } as const;

/** Ingestion status. The failure reason (if any) is available as a tooltip. */
export function StatusBadge({ status, error }: { status: DocumentStatus; error?: string | null }) {
  const icon = {
    queued: <Clock aria-hidden className="size-3" />,
    processing: <Loader2 aria-hidden className="size-3 animate-spin" />,
    indexed: <CheckCircle2 aria-hidden className="size-3" />,
    failed: <AlertCircle aria-hidden className="size-3" />,
  }[status];
  return (
    <Badge
      tone={TONES[status]}
      data-testid="document-status"
      data-status={status}
      title={status === "failed" && error ? error : undefined}
    >
      {icon}
      {DOCUMENT_STATUS_LABELS[status]}
    </Badge>
  );
}
