import type {
  AnswerStatus,
  ChatScope,
  Citation,
  Claim,
  DocumentChange,
  Message,
  MessageStatus,
  SourceRef,
} from "@/lib/api/types";
import type { Scope } from "@/components/shared/scope-picker";

/** What the thread renders for one message, whether it came from the API or from a live stream. */
export interface DisplayMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: MessageStatus;
  answerStatus: AnswerStatus | null;
  sources: SourceRef[];
  citations: Citation[];
  /** Each statement checked against the passages it cites (absent while streaming). */
  claims?: Claim[];
  /** Cited documents replaced by a newer version since the answer (history only). */
  changedDocuments?: DocumentChange[];
  /** The answer drew on a document the reader can no longer open, so its text is withheld. */
  redacted?: boolean;
  /** Set while the answer is still being produced. */
  streaming?: boolean;
  /** Stage text shown before the first token ("Searching your documents..."). */
  stage?: string;
  error?: { message: string; requestId: string | null };
}

export const fromMessage = (message: Message): DisplayMessage => ({
  id: message.id,
  role: message.role,
  content: message.content,
  status: message.status,
  answerStatus: message.answer_status ?? null,
  sources: message.sources ?? [],
  citations: message.citations ?? [],
  claims: message.claims ?? [],
  changedDocuments: message.changed_documents ?? [],
  redacted: message.redacted,
});

export function toChatScope(scope: Scope): ChatScope {
  return {
    workspace_ids: scope.workspaceIds.length ? scope.workspaceIds : null,
    document_ids: scope.documents.length ? scope.documents.map((d) => d.id) : null,
  };
}
