"use client";

import { memo } from "react";
import { BookOpen, RotateCcw, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { Spinner } from "@/components/ui/feedback";
import type { DisplayMessage } from "@/lib/chat-state";
import { AnswerBanner, answerNotice } from "./answer-banner";
import { AnswerReceipt, ReplacedSources } from "./answer-receipt";
import { MarkdownAnswer } from "./markdown-answer";

interface Props {
  message: DisplayMessage;
  /** Only the latest answer offers Retry. */
  canRetry: boolean;
  onCite: (messageId: string, sourceId: string) => void;
  onOpenSources: (messageId: string) => void;
  onRetry: () => void;
}

export const MessageView = memo(function MessageView({ message, canRetry, onCite, onOpenSources, onRetry }: Props) {
  if (message.role === "user") {
    return (
      <article aria-label="Your question" data-testid="user-message" className="flex justify-end">
        <p className="max-w-[85%] rounded-2xl rounded-tr-sm bg-accent-600 px-4 py-2.5 text-[15px] leading-relaxed break-words whitespace-pre-wrap text-white">
          {message.content}
        </p>
      </article>
    );
  }

  const waiting = message.streaming && !message.content;
  const notice = answerNotice(message);
  const settled = !message.streaming && !message.redacted;
  const claims = settled ? (message.claims ?? []) : [];
  const changes = settled ? (message.changedDocuments ?? []) : [];
  return (
    <article
      aria-label="Assistant answer"
      data-testid="assistant-message"
      data-status={message.streaming ? "streaming" : message.error ? "error" : message.status}
      className="flex gap-3"
    >
      <span aria-hidden className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-full bg-slate-900 text-white">
        <Sparkles className="size-3.5" />
      </span>
      <div className="min-w-0 flex-1">
        {changes.length ? <ReplacedSources changes={changes} className="mb-3" /> : null}
        {notice?.placement === "top" ? <AnswerBanner notice={notice} className="mb-3" /> : null}
        {waiting ? (
          <p className="flex items-center gap-2 text-sm text-slate-500" data-testid="answer-stage">
            <Spinner label={message.stage} /> {message.stage}
          </p>
        ) : message.content ? (
          <div data-testid="assistant-answer" aria-busy={message.streaming || undefined} aria-live="off">
            <MarkdownAnswer
              text={message.content}
              sources={message.sources}
              citations={message.citations}
              onCite={(sourceId) => onCite(message.id, sourceId)}
              streaming={message.streaming}
            />
          </div>
        ) : null}
        {claims.length ? <AnswerReceipt claims={claims} sources={message.sources} /> : null}
        {notice?.placement === "bottom" ? <AnswerBanner notice={notice} className="mt-3" /> : null}

        {!message.streaming ? (
          <div className="mt-2 -ml-2 flex flex-wrap items-center gap-1">
            {message.content ? <CopyButton text={message.content} label="Copy answer" /> : null}
            {message.sources.length > 0 ? (
              <Button size="sm" variant="ghost" onClick={() => onOpenSources(message.id)} data-testid="open-sources">
                <BookOpen aria-hidden className="size-4" /> Sources ({message.sources.length})
              </Button>
            ) : null}
            {canRetry ? (
              <Button size="sm" variant="ghost" onClick={onRetry} data-testid="retry-answer">
                <RotateCcw aria-hidden className="size-4" /> Retry
              </Button>
            ) : null}
          </div>
        ) : message.sources.length > 0 ? (
          <div className="mt-2 -ml-2">
            <Button size="sm" variant="ghost" onClick={() => onOpenSources(message.id)} data-testid="open-sources">
              <BookOpen aria-hidden className="size-4" /> Sources ({message.sources.length})
            </Button>
          </div>
        ) : null}
      </div>
    </article>
  );
});
