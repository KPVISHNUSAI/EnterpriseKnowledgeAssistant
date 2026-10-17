"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Scope } from "@/components/shared/scope-picker";
import { StreamUnavailableError, streamChat } from "@/lib/api/chat-stream";
import { api } from "@/lib/api/endpoints";
import { errorMessage, errorRequestId, isAbortError } from "@/lib/api/errors";
import type { ChatRequest, Message, SourceRef } from "@/lib/api/types";
import { toChatScope } from "@/lib/chat-state";
import { keys } from "./keys";

export type Phase = "retrieving" | "generating" | "done" | "error" | "stopped";

/** The question being answered right now (or just finished), before the server copy is loaded. */
export interface LiveTurn {
  /** Conversation the turn was sent in (null: a new chat) and the id the server created for it. */
  startedIn: string | null;
  createdId: string | null;
  userMessageId: string | null;
  assistantMessageId: string | null;
  userText: string;
  text: string;
  sources: SourceRef[];
  phase: Phase;
  /** The persisted assistant message, once the stream reported `done`. */
  message: Message | null;
  error: { message: string; requestId: string | null } | null;
}

/** True when the turn belongs to the conversation currently shown (also during new-chat navigation). */
export function turnBelongsTo(turn: LiveTurn, routeConversationId: string | undefined): boolean {
  return (
    turn.startedIn === (routeConversationId ?? null) ||
    (turn.createdId !== null && turn.createdId === routeConversationId)
  );
}

export const isBusy = (turn: LiveTurn | null) => turn?.phase === "retrieving" || turn?.phase === "generating";

const TOKEN_FLUSH_MS = 30;
const STOP_REFRESH_MS = 1200;

/**
 * Sends a question and folds the SSE events (meta, sources, token, done, error) into `live`.
 * Tokens are batched so a fast model does not re-render the Markdown hundreds of times a second.
 */
export function useChatStream({
  conversationId,
  scope,
  onConversationCreated,
}: {
  conversationId: string | undefined;
  scope: Scope;
  onConversationCreated: (id: string) => void;
}) {
  const client = useQueryClient();
  const [live, setLive] = useState<LiveTurn | null>(null);
  const controller = useRef<AbortController | null>(null);
  const pending = useRef("");
  const flushTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const patch = useCallback(
    (changes: Partial<LiveTurn> | ((turn: LiveTurn) => Partial<LiveTurn>)) =>
      setLive((turn) => (turn ? { ...turn, ...(typeof changes === "function" ? changes(turn) : changes) } : turn)),
    [],
  );

  const flush = useCallback(() => {
    clearTimeout(flushTimer.current);
    flushTimer.current = undefined;
    const chunk = pending.current;
    pending.current = "";
    if (chunk)
      patch((turn) => ({ text: turn.text + chunk, phase: turn.phase === "retrieving" ? "generating" : turn.phase }));
  }, [patch]);

  const queueToken = useCallback(
    (text: string) => {
      pending.current += text;
      flushTimer.current ??= setTimeout(flush, TOKEN_FLUSH_MS);
    },
    [flush],
  );

  useEffect(
    () => () => {
      controller.current?.abort();
      clearTimeout(flushTimer.current);
    },
    [],
  );

  const refresh = useCallback(
    (id: string) =>
      Promise.all([
        client.invalidateQueries({ queryKey: keys.messages(id) }),
        client.invalidateQueries({ queryKey: keys.conversations }),
        client.invalidateQueries({ queryKey: keys.dashboard }),
      ]),
    [client],
  );

  const send = useCallback(
    async (text: string) => {
      if (controller.current) return;
      const abort = new AbortController();
      controller.current = abort;
      pending.current = "";
      setLive({
        startedIn: conversationId ?? null,
        createdId: null,
        userMessageId: null,
        assistantMessageId: null,
        userText: text,
        text: "",
        sources: [],
        phase: "retrieving",
        message: null,
        error: null,
      });
      const request: ChatRequest = {
        message: text,
        conversation_id: conversationId ?? null,
        scope: toChatScope(scope),
      };
      let known = conversationId ?? null;
      let stopped = false;
      const adopt = (id: string) => {
        if (known === id) return;
        known = id;
        onConversationCreated(id);
        void client.invalidateQueries({ queryKey: keys.conversations }); // show the new chat in the list right away
      };

      try {
        try {
          for await (const item of streamChat(request, abort.signal)) {
            if (item.event === "meta") {
              adopt(item.data.conversation_id);
              patch({
                createdId: item.data.conversation_id,
                userMessageId: item.data.user_message_id,
                assistantMessageId: item.data.assistant_message_id,
              });
            } else if (item.event === "sources") {
              patch({ sources: item.data.sources, phase: "generating" });
            } else if (item.event === "token") {
              queueToken(item.data.text);
            } else if (item.event === "done") {
              flush();
              patch({
                phase: "done",
                message: item.data.message,
                text: item.data.message.content,
                sources: item.data.message.sources ?? [],
              });
            } else if (item.event === "error") {
              flush();
              patch({ phase: "error", error: { message: item.data.message, requestId: null } });
            }
          }
        } catch (error) {
          if (!(error instanceof StreamUnavailableError)) throw error;
          // The response was not an event stream (proxy or old browser): use the plain endpoint.
          const response = await api.chat.send(request, abort.signal);
          adopt(response.conversation_id);
          patch({
            createdId: response.conversation_id,
            userMessageId: response.user_message.id,
            assistantMessageId: response.assistant_message.id,
            phase: "done",
            message: response.assistant_message,
            text: response.assistant_message.content,
            sources: response.assistant_message.sources ?? [],
          });
        }
      } catch (error) {
        flush();
        if (isAbortError(error)) {
          stopped = true;
          patch({ phase: "stopped" });
        } else patch({ phase: "error", error: { message: errorMessage(error), requestId: errorRequestId(error) } });
      } finally {
        flush();
        controller.current = null;
        if (known) {
          const id = known;
          void refresh(id);
          // After a stop the server saves the partial answer a moment later.
          if (stopped) setTimeout(() => void refresh(id), STOP_REFRESH_MS);
        }
      }
    },
    [conversationId, scope, onConversationCreated, patch, queueToken, flush, refresh, client],
  );

  const stop = useCallback(() => controller.current?.abort(), []);

  return { live, send, stop, isStreaming: isBusy(live) };
}
