"use client";

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useToast } from "@/components/ui/toast";
import { api } from "@/lib/api/endpoints";
import type { Message } from "@/lib/api/types";
import { keys } from "./keys";

const CONVERSATIONS_PAGE = 30;
const MESSAGES_PAGE = 100; // API maximum
const MAX_MESSAGE_PAGES = 20;

export function useConversations() {
  return useInfiniteQuery({
    queryKey: keys.conversations,
    queryFn: ({ pageParam }) => api.chat.conversations({ page: pageParam, page_size: CONVERSATIONS_PAGE }),
    initialPageParam: 1,
    getNextPageParam: (last) => (last.page * last.page_size < last.total ? last.page + 1 : undefined),
  });
}

/** Every message of a conversation in chronological order (fetches all pages). */
export function useMessages(conversationId: string | undefined) {
  return useQuery({
    queryKey: keys.messages(conversationId ?? ""),
    enabled: Boolean(conversationId),
    queryFn: async () => {
      const messages: Message[] = [];
      for (let page = 1; page <= MAX_MESSAGE_PAGES; page++) {
        const result = await api.chat.messages(conversationId!, { page, page_size: MESSAGES_PAGE });
        messages.push(...result.items);
        if (messages.length >= result.total || result.items.length === 0) break;
      }
      return messages;
    },
  });
}

export function useDeleteConversation() {
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: api.chat.deleteConversation,
    onSuccess: async () => {
      toast.success("Conversation deleted");
      await Promise.all([
        client.invalidateQueries({ queryKey: keys.conversations }),
        client.invalidateQueries({ queryKey: keys.dashboard }),
      ]);
    },
  });
}
