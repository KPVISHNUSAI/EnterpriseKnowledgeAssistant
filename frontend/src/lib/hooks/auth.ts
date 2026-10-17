"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/endpoints";
import type { Me } from "@/lib/api/types";
import { keys } from "./keys";

export function useMe({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: keys.me,
    queryFn: ({ signal }) => api.auth.me(signal),
    staleTime: 5 * 60_000,
    retry: false,
    enabled,
  });
}

/** Store a fresh session/identity (after login, register, switching organizations...). */
export function useSetMe() {
  const client = useQueryClient();
  return (me: Me) => {
    client.removeQueries({ predicate: (query) => query.queryKey[0] !== keys.me[0] });
    client.setQueryData(keys.me, me);
  };
}

/** Sign out, drop every cached query and hard-navigate (default: the sign-in page). */
export function useSignOut() {
  const client = useQueryClient();
  const logout = useMutation({ mutationFn: api.auth.logout, meta: { inline: true } });
  return {
    pending: logout.isPending,
    signOut: (redirectTo = "/login") =>
      logout.mutate(undefined, {
        onSettled: () => {
          client.clear();
          window.location.assign(redirectTo);
        },
      }),
  };
}

export function useSwitchOrganization() {
  const setMe = useSetMe();
  return useMutation({
    mutationFn: api.auth.switchOrganization,
    onSuccess: setMe,
  });
}
