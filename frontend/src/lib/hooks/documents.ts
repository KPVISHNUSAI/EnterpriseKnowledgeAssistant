"use client";

import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useToast } from "@/components/ui/toast";
import { api } from "@/lib/api/endpoints";
import type { DocumentListQuery } from "@/lib/api/types";
import { isActiveStatus } from "@/lib/labels";
import { keys } from "./keys";

const POLL_MS = 3000;

/** The document list; refreshes every few seconds while anything is queued or processing. */
export function useDocuments(query: DocumentListQuery, { enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: keys.documentList(query),
    queryFn: () => api.documents.list(query),
    enabled,
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (q.state.data?.items.some((d) => isActiveStatus(d.processing_status)) ? POLL_MS : false),
  });
}

export function useDocument(id: string) {
  return useQuery({
    queryKey: keys.document(id),
    queryFn: () => api.documents.get(id),
    refetchInterval: (q) => (q.state.data && isActiveStatus(q.state.data.processing_status) ? POLL_MS : false),
  });
}

/** Extracted text, loaded page by page (100 chunks at a time). */
export function useDocumentChunks(id: string, indexedAt: string | null, enabled: boolean) {
  return useInfiniteQuery({
    queryKey: keys.chunks(id, indexedAt),
    queryFn: ({ pageParam }) => api.documents.chunks(id, { page: pageParam, page_size: 100 }),
    initialPageParam: 1,
    getNextPageParam: (last) => (last.page * last.page_size < last.total ? last.page + 1 : undefined),
    enabled,
  });
}

/** Everything that shows document counts or lists depends on these. */
function useInvalidateDocuments() {
  const client = useQueryClient();
  return () =>
    Promise.all([
      client.invalidateQueries({ queryKey: keys.documents }),
      client.invalidateQueries({ queryKey: keys.dashboard }),
      client.invalidateQueries({ queryKey: keys.workspaces }),
    ]);
}

export function useReindexDocument() {
  const toast = useToast();
  const invalidate = useInvalidateDocuments();
  return useMutation({
    mutationFn: api.documents.reindex,
    onSuccess: () => {
      toast.success("Re-indexing queued");
      return invalidate();
    },
  });
}

export function useDeleteDocument() {
  const toast = useToast();
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.documents.remove,
    onSuccess: () => {
      toast.success("Document deleted");
      // Lists and counts only: refetching the deleted document's own detail would just 404.
      return Promise.all([
        client.invalidateQueries({ queryKey: [...keys.documents, "list"] }),
        client.invalidateQueries({ queryKey: keys.dashboard }),
        client.invalidateQueries({ queryKey: keys.workspaces }),
      ]);
    },
  });
}

export function useRenameDocument(id: string) {
  const toast = useToast();
  const invalidate = useInvalidateDocuments();
  return useMutation({
    mutationFn: (title: string) => api.documents.rename(id, title),
    onSuccess: () => {
      toast.success("Document renamed");
      return invalidate();
    },
  });
}

export function useUpdatePermissions(id: string) {
  const toast = useToast();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Parameters<typeof api.documents.updatePermissions>[1]) =>
      api.documents.updatePermissions(id, body),
    onSuccess: (detail) => {
      toast.success("Sharing updated");
      client.setQueryData(keys.document(id), detail);
      return client.invalidateQueries({ queryKey: keys.documents });
    },
  });
}

/** Let a document's text addressed to AI assistants into answers, or keep it out again. */
export function useSetFlaggedText(id: string) {
  const toast = useToast();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ included, version }: { included: boolean; version: number }) =>
      api.documents.setFlaggedText(id, included, version),
    onSuccess: (detail) => {
      toast.success(
        detail.flagged_text_included ? "Flagged text included in answers" : "Flagged text removed from answers",
      );
      client.setQueryData(keys.document(id), detail);
      return client.invalidateQueries({ queryKey: keys.documents });
    },
    // 409 version_changed: a newer version arrived, so show it before anyone decides again.
    onError: () => client.invalidateQueries({ queryKey: keys.documents }),
  });
}

export function useInvalidateAfterUpload() {
  return useInvalidateDocuments();
}
