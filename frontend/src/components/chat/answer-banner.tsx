import { Alert } from "@/components/ui/feedback";
import type { DisplayMessage } from "@/lib/chat-state";

interface Notice {
  tone: "info" | "warning" | "danger";
  title: string;
  body: string;
  status: string;
  /** Verdicts about the content come before it; how the stream ended comes after it. */
  placement: "top" | "bottom";
  requestId?: string | null;
}

/** Explains non-standard outcomes so a weak answer is never mistaken for a confident one. */
export function answerNotice(message: DisplayMessage): Notice | null {
  const { answerStatus, status } = message;
  if (message.error) {
    return {
      tone: "danger",
      title: "The answer could not be completed",
      body: message.error.message,
      status: "error",
      placement: "bottom",
      requestId: message.error.requestId,
    };
  }
  if (message.redacted) {
    return {
      tone: "info",
      title: "Answer hidden",
      body: "This answer drew on a document you can no longer open, so its text is no longer shown.",
      status: "redacted",
      placement: "top",
    };
  }
  switch (answerStatus) {
    case "insufficient_evidence":
      return {
        tone: "info",
        title: "Not enough evidence in your documents",
        body: "The documents you can access do not support an answer. Try rephrasing, widening the scope, or check that the relevant documents have finished indexing.",
        status: answerStatus,
        placement: "top",
      };
    case "partial":
      return {
        tone: "warning",
        title: "Partial answer",
        body: "The documents only support part of this question. Parts without a citation are not backed by your documents.",
        status: answerStatus,
        placement: "top",
      };
    case "unverified":
      return {
        tone: "warning",
        title: "Unverified answer",
        body: "The citations in this answer could not be matched to the retrieved sources. Treat it with caution and check the documents yourself.",
        status: answerStatus,
        placement: "top",
      };
    case "refused":
      return {
        tone: "warning",
        title: "The model declined to answer",
        body: "The request was refused by the language model's safety policy. Try rephrasing your question.",
        status: answerStatus,
        placement: "top",
      };
    case "error":
      return {
        tone: "danger",
        title: "Something went wrong while generating this answer",
        body: "Please try again. If the problem continues, contact your administrator.",
        status: answerStatus,
        placement: "bottom",
      };
  }
  if (status === "error") {
    return {
      tone: "danger",
      title: "Something went wrong while generating this answer",
      body: "Please try again. If the problem continues, contact your administrator.",
      status: "error",
      placement: "bottom",
    };
  }
  if (status === "incomplete") {
    return {
      tone: "info",
      title: "Generation stopped",
      body: "This answer is incomplete.",
      status: "incomplete",
      placement: "bottom",
    };
  }
  return null;
}

export function AnswerBanner({ notice, className }: { notice: Notice; className?: string }) {
  return (
    <Alert
      tone={notice.tone}
      title={notice.title}
      className={className}
      data-testid="answer-banner"
      data-status={notice.status}
    >
      {notice.body}
      {notice.requestId ? (
        <p className="mt-1 text-xs opacity-80">
          Request ID: <code className="font-mono select-all">{notice.requestId}</code>
        </p>
      ) : null}
    </Alert>
  );
}
