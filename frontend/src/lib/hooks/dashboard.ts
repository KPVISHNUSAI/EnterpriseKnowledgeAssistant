"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api/endpoints";
import { keys } from "./keys";

/** Overview data; refreshes while documents are still being indexed. */
export function useDashboard() {
  return useQuery({
    queryKey: keys.dashboard,
    queryFn: api.dashboard,
    refetchInterval: (q) => {
      const by = q.state.data?.documents_by_status;
      return (by?.queued ?? 0) + (by?.processing ?? 0) > 0 ? 5000 : false;
    },
  });
}
