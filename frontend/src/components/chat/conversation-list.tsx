"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { MessageSquare, Plus, Trash2 } from "lucide-react";
import { Button, IconButton, LinkButton } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { ErrorNotice, SkeletonRows } from "@/components/ui/feedback";
import { Time } from "@/components/ui/time";
import { cn } from "@/lib/cn";
import { useConversations, useDeleteConversation } from "@/lib/hooks/chat";

export function ConversationList({ activeId, onNavigate }: { activeId: string | undefined; onNavigate?: () => void }) {
  const router = useRouter();
  const confirm = useConfirm();
  const query = useConversations();
  const remove = useDeleteConversation();
  const conversations = query.data?.pages.flatMap((page) => page.items) ?? [];

  async function onDelete(id: string, title: string) {
    const ok = await confirm({
      title: "Delete this conversation?",
      description: `“${title}” and all of its messages will be permanently deleted.`,
      confirmLabel: "Delete conversation",
      destructive: true,
    });
    if (!ok) return;
    remove.mutate(id, { onSuccess: () => id === activeId && router.replace("/chat") });
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="p-3">
        <LinkButton href="/chat" variant="primary" className="w-full" onClick={onNavigate} data-testid="new-chat">
          <Plus aria-hidden className="size-4" /> New chat
        </LinkButton>
      </div>
      <nav aria-label="Conversations" className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {query.isError ? (
          <ErrorNotice error={query.error} title="Could not load conversations" onRetry={() => query.refetch()} />
        ) : query.isPending ? (
          <SkeletonRows rows={5} className="px-1" />
        ) : conversations.length === 0 ? (
          <div className="px-3 py-8 text-center text-sm text-slate-500">
            <MessageSquare aria-hidden className="mx-auto mb-2 size-6 text-slate-300" />
            No conversations yet.
          </div>
        ) : (
          <ul className="space-y-0.5" data-testid="conversation-list">
            {conversations.map((conversation) => {
              const active = conversation.id === activeId;
              return (
                <li key={conversation.id} className="group relative">
                  <Link
                    href={`/chat/${conversation.id}`}
                    onClick={onNavigate}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "block rounded-md py-2 pr-9 pl-3 text-sm hover:bg-slate-100",
                      active ? "bg-accent-50 text-accent-900" : "text-slate-700",
                    )}
                  >
                    <span className="block truncate font-medium">{conversation.title}</span>
                    <span className="block text-xs text-slate-500">
                      <Time iso={conversation.updated_at} />
                    </span>
                  </Link>
                  <IconButton
                    label={`Delete conversation ${conversation.title}`}
                    onClick={() => onDelete(conversation.id, conversation.title)}
                    className="absolute top-1/2 right-1 -translate-y-1/2 opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100"
                  >
                    <Trash2 aria-hidden className="size-4" />
                  </IconButton>
                </li>
              );
            })}
          </ul>
        )}
        {query.hasNextPage ? (
          <Button
            size="sm"
            variant="ghost"
            className="mt-2 w-full"
            onClick={() => query.fetchNextPage()}
            disabled={query.isFetchingNextPage}
          >
            {query.isFetchingNextPage ? "Loading..." : "Load more"}
          </Button>
        ) : null}
      </nav>
    </div>
  );
}
