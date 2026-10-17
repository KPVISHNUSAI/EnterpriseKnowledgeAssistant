"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useToast } from "@/components/ui/toast";
import { api } from "@/lib/api/endpoints";
import type { WorkspaceRole } from "@/lib/api/types";
import { keys } from "./keys";

export function useWorkspaces() {
  return useQuery({ queryKey: keys.workspaces, queryFn: api.workspaces.list });
}

export function useWorkspace(id: string) {
  return useQuery({ queryKey: keys.workspace(id), queryFn: () => api.workspaces.get(id) });
}

export function useWorkspaceMembers(id: string) {
  return useQuery({ queryKey: keys.workspaceMembers(id), queryFn: () => api.workspaces.members(id) });
}

/** Organization directory search (picking workspace members, document grantees, uploaders). */
export function useOrgMembers(params: { q?: string; page?: number; page_size?: number }) {
  return useQuery({
    queryKey: keys.orgMembers(params),
    queryFn: () => api.organization.members(params),
    placeholderData: keepPreviousData,
  });
}

export function useCreateWorkspace() {
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    meta: { inline: true }, // the dialog shows errors such as "name already taken"
    mutationFn: api.workspaces.create,
    onSuccess: async (workspace) => {
      toast.success(`Workspace “${workspace.name}” created`);
      await Promise.all([
        client.invalidateQueries({ queryKey: keys.workspaces }),
        client.invalidateQueries({ queryKey: keys.me }), // the workspace list in /auth/me
      ]);
    },
  });
}

export function useUpdateWorkspace(id: string) {
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { name?: string; description?: string | null }) => api.workspaces.update(id, body),
    onSuccess: async () => {
      toast.success("Workspace updated");
      await Promise.all([
        client.invalidateQueries({ queryKey: keys.workspaces }),
        client.invalidateQueries({ queryKey: keys.me }),
      ]);
    },
  });
}

export function useDeleteWorkspace() {
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: api.workspaces.remove,
    onSuccess: async () => {
      toast.success("Workspace deleted");
      await Promise.all([
        client.invalidateQueries({ queryKey: keys.workspaces }),
        client.invalidateQueries({ queryKey: keys.me }),
      ]);
    },
  });
}

export function useWorkspaceMemberActions(workspaceId: string) {
  const client = useQueryClient();
  const toast = useToast();
  const refresh = () =>
    Promise.all([
      client.invalidateQueries({ queryKey: keys.workspaceMembers(workspaceId) }),
      client.invalidateQueries({ queryKey: keys.workspaces }),
      client.invalidateQueries({ queryKey: keys.me }),
    ]);
  return {
    add: useMutation({
      meta: { inline: true },
      mutationFn: (body: { user_id: string; role: WorkspaceRole }) => api.workspaces.addMember(workspaceId, body),
      onSuccess: () => {
        toast.success("Member added");
        return refresh();
      },
    }),
    changeRole: useMutation({
      meta: { inline: true }, // the members table shows the error (for example "last owner")
      mutationFn: (v: { userId: string; role: WorkspaceRole }) =>
        api.workspaces.updateMember(workspaceId, v.userId, v.role),
      onSuccess: () => {
        toast.success("Role updated");
        return refresh();
      },
    }),
    remove: useMutation({
      meta: { inline: true },
      mutationFn: (userId: string) => api.workspaces.removeMember(workspaceId, userId),
      onSuccess: () => {
        toast.success("Member removed");
        return refresh();
      },
    }),
  };
}
