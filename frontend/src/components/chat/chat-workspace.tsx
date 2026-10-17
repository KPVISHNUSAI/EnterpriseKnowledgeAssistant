"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { MessageSquarePlus, PanelLeft, Sparkles } from "lucide-react";
import { EMPTY_SCOPE, type Scope } from "@/components/shared/scope-picker";
import { Button, LinkButton } from "@/components/ui/button";
import { Drawer } from "@/components/ui/dialog";
import { EmptyState, ErrorNotice, Skeleton } from "@/components/ui/feedback";
import { ApiError } from "@/lib/api/errors";
import { fromMessage, type DisplayMessage } from "@/lib/chat-state";
import { useConversations, useMessages } from "@/lib/hooks/chat";
import { turnBelongsTo, useChatStream, type LiveTurn } from "@/lib/hooks/chat-stream";
import { useMediaQuery } from "@/lib/hooks/media-query";
import { Composer } from "./composer";
import { ConversationList } from "./conversation-list";
import { MessageView } from "./message-view";
import { SourcePanel } from "./source-panel";

/** The in-flight (or just finished) question as thread messages. */
function liveMessages(live: LiveTurn): DisplayMessage[] {
  const user: DisplayMessage = {
    id: live.userMessageId ?? "pending-user",
    role: "user",
    content: live.userText,
    status: "complete",
    answerStatus: null,
    sources: [],
    citations: [],
  };
  const id = live.assistantMessageId ?? "pending-assistant";
  const busy = live.phase === "retrieving" || live.phase === "generating";
  const assistant: DisplayMessage =
    live.phase === "done" && live.message
      ? fromMessage(live.message)
      : {
          id,
          role: "assistant",
          content: live.text,
          status: live.phase === "error" ? "error" : live.phase === "stopped" ? "incomplete" : "streaming",
          answerStatus: null,
          sources: live.sources,
          citations: [],
          streaming: busy,
          stage: live.phase === "retrieving" ? "Searching your documents..." : "Writing the answer...",
          error: live.phase === "error" && live.error ? live.error : undefined,
        };
  return [user, assistant];
}

function announcement(live: LiveTurn | null): string {
  if (!live) return "";
  if (live.phase === "retrieving" || live.phase === "generating") return "Generating answer";
  if (live.phase === "done") return "Answer ready";
  if (live.phase === "error") return "The answer could not be completed";
  return "Generation stopped";
}

/**
 * The whole chat experience. It lives in the /chat layout, so a conversation that is being
 * created keeps streaming while the URL changes from /chat to /chat/<new id>.
 */
export function ChatWorkspace() {
  const router = useRouter();
  const routeId = useParams<{ conversationId?: string }>().conversationId;
  const desktop = useMediaQuery("(min-width: 1024px)");
  const wide = useMediaQuery("(min-width: 1536px)");
  const [scope, setScope] = useState<Scope>(EMPTY_SCOPE);
  const [panel, setPanel] = useState<{ messageId: string; sourceId: string | null } | null>(null);
  const [listOpen, setListOpen] = useState(false);

  const onCreated = useCallback((id: string) => router.replace(`/chat/${id}`), [router]);
  const { live, send, stop, isStreaming } = useChatStream({
    conversationId: routeId,
    scope,
    onConversationCreated: onCreated,
  });
  const history = useMessages(routeId);
  const conversations = useConversations();

  const liveHere = live && turnBelongsTo(live, routeId) ? live : null;
  const persisted = useMemo(() => (history.data ?? []).map(fromMessage), [history.data]);
  const thread = useMemo(() => {
    if (!liveHere) return persisted;
    const overlay = liveMessages(liveHere);
    const replaced = new Set(overlay.map((m) => m.id));
    return [...persisted.filter((m) => !replaced.has(m.id)), ...overlay];
  }, [persisted, liveHere]);

  // Leaving a conversation while it is answering stops that answer (the partial text is saved).
  useEffect(() => {
    if (isStreaming && live && !turnBelongsTo(live, routeId)) stop();
  }, [routeId, isStreaming, live, stop]);

  // Keep the newest text in view unless the reader has scrolled up.
  const scroller = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  useEffect(() => {
    stickToBottom.current = true;
  }, [routeId]);
  useEffect(() => {
    const el = scroller.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [thread, routeId]);

  const lastUserText = [...thread].reverse().find((m) => m.role === "user")?.content;
  const lastAssistantId = [...thread].reverse().find((m) => m.role === "assistant")?.id;
  const panelMessage = panel ? thread.find((m) => m.id === panel.messageId) : undefined;
  const title = conversations.data?.pages.flatMap((p) => p.items).find((c) => c.id === routeId)?.title;

  const sendMessage = useCallback(
    (text: string) => {
      stickToBottom.current = true;
      void send(text);
    },
    [send],
  );
  const cite = useCallback((messageId: string, sourceId: string) => setPanel({ messageId, sourceId }), []);
  const openSources = useCallback((messageId: string) => setPanel({ messageId, sourceId: null }), []);
  const retry = useCallback(() => {
    if (lastUserText) sendMessage(lastUserText);
  }, [lastUserText, sendMessage]);

  const showHistoryState = routeId && history.isPending && !liveHere;
  const notFound = history.error instanceof ApiError && history.error.status === 404;

  const inlineList = desktop && (wide || !panelMessage);
  const panelContent = panelMessage ? (
    <SourcePanel
      sources={panelMessage.sources}
      selectedId={panel?.sourceId ?? null}
      onClose={() => setPanel(null)}
      showHeader={desktop}
    />
  ) : null;

  return (
    <div className="flex h-full min-h-0 bg-white">
      {inlineList ? (
        <aside className="w-72 shrink-0 border-r border-slate-200 bg-slate-50/60">
          <ConversationList activeId={routeId} />
        </aside>
      ) : (
        <Drawer open={listOpen} onOpenChange={setListOpen} title="Conversations">
          <ConversationList activeId={routeId} onNavigate={() => setListOpen(false)} />
        </Drawer>
      )}

      <section aria-label="Conversation" className="flex min-w-0 flex-1 flex-col">
        <title>{`${routeId ? (title ?? "Conversation") : "New chat"} | Knowledge Assistant`}</title>
        <div className="flex h-12 shrink-0 items-center gap-2 border-b border-slate-200 px-3 sm:px-4">
          {!inlineList ? (
            <Button size="sm" variant="ghost" onClick={() => setListOpen(true)}>
              <PanelLeft aria-hidden className="size-4" /> Conversations
            </Button>
          ) : null}
          <h1 className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-900">
            {routeId ? (title ?? "Conversation") : "New chat"}
          </h1>
          {routeId ? (
            <LinkButton href="/chat" size="sm" variant="ghost">
              <MessageSquarePlus aria-hidden className="size-4" /> New chat
            </LinkButton>
          ) : null}
        </div>

        <div
          ref={scroller}
          onScroll={() => {
            const el = scroller.current;
            if (el) stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          }}
          className="min-h-0 flex-1 overflow-y-auto"
          data-testid="chat-thread"
        >
          <div className="mx-auto max-w-3xl space-y-6 px-4 py-6">
            {showHistoryState ? (
              <div className="space-y-4" role="status" aria-label="Loading conversation">
                <Skeleton className="ml-auto h-10 w-2/3" />
                <Skeleton className="h-28 w-full" />
              </div>
            ) : history.isError && !liveHere ? (
              notFound ? (
                <EmptyState
                  title="Conversation not found"
                  description="It may have been deleted."
                  action={
                    <LinkButton href="/chat" variant="primary">
                      Start a new chat
                    </LinkButton>
                  }
                />
              ) : (
                <ErrorNotice
                  error={history.error}
                  title="Could not load this conversation"
                  onRetry={() => history.refetch()}
                />
              )
            ) : thread.length === 0 ? (
              <div className="py-16 text-center">
                <span className="mx-auto mb-4 grid size-12 place-items-center rounded-2xl bg-accent-50 text-accent-700">
                  <Sparkles aria-hidden className="size-6" />
                </span>
                <h2 className="text-lg font-semibold text-slate-900">Ask your documents</h2>
                <p className="mx-auto mt-1 max-w-md text-sm text-slate-600">
                  Answers are grounded in the documents you can access and cite their sources. Use the scope button to
                  focus on specific workspaces or documents.
                </p>
              </div>
            ) : (
              thread.map((message) => (
                <MessageView
                  key={message.id}
                  message={message}
                  canRetry={message.id === lastAssistantId && !isStreaming && Boolean(lastUserText)}
                  onCite={cite}
                  onOpenSources={openSources}
                  onRetry={retry}
                />
              ))
            )}
          </div>
        </div>
        <div className="sr-only" role="status" aria-live="polite">
          {announcement(liveHere)}
        </div>
        <Composer scope={scope} onScopeChange={setScope} streaming={isStreaming} onSend={sendMessage} onStop={stop} />
      </section>

      {desktop ? (
        panelContent ? (
          <aside className="w-96 shrink-0 border-l border-slate-200">{panelContent}</aside>
        ) : null
      ) : (
        <Drawer
          open={Boolean(panelContent)}
          onOpenChange={(open) => !open && setPanel(null)}
          title="Sources"
          side="right"
        >
          {panelContent}
        </Drawer>
      )}
    </div>
  );
}
