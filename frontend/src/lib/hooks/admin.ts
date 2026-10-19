"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useToast } from "@/components/ui/toast";
import { api } from "@/lib/api/endpoints";
import type { AuditQuery, OrgRole } from "@/lib/api/types";
import { keys } from "./keys";

export function useUsage(days: number) {
  return useQuery({
    queryKey: keys.usage(days),
    queryFn: () => api.admin.usage(days),
    placeholderData: keepPreviousData,
  });
}

export function useAuditEvents(query: AuditQuery) {
  return useQuery({
    queryKey: keys.audit(query),
    queryFn: () => api.admin.auditEvents(query),
    placeholderData: keepPreviousData,
  });
}

export function useInvitations(query: { page: number; page_size: number }) {
  return useQuery({
    queryKey: keys.invitations(query),
    queryFn: () => api.organization.invitations(query),
    placeholderData: keepPreviousData,
  });
}

const ORG_KEYS = [["org-members"], ["invitations"], ["admin"], ["me"]] as const;

function useRefreshOrganization() {
  const client = useQueryClient();
  return () => Promise.all(ORG_KEYS.map((queryKey) => client.invalidateQueries({ queryKey })));
}

export function useCreateInvitation() {
  const refresh = useRefreshOrganization();
  return useMutation({
    meta: { inline: true }, // the dialog shows errors such as "already a member"
    mutationFn: api.organization.createInvitation,
    onSuccess: refresh,
  });
}

export function useRevokeInvitation() {
  const toast = useToast();
  const refresh = useRefreshOrganization();
  return useMutation({
    mutationFn: api.organization.revokeInvitation,
    onSuccess: () => {
      toast.success("Invitation revoked");
      return refresh();
    },
  });
}

export function useOrgMemberActions() {
  const toast = useToast();
  const refresh = useRefreshOrganization();
  return {
    changeRole: useMutation({
      meta: { inline: true }, // "cannot remove the last admin" is shown next to the table
      mutationFn: (v: { userId: string; role: OrgRole }) => api.organization.updateMemberRole(v.userId, v.role),
      onSuccess: () => {
        toast.success("Role updated");
        return refresh();
      },
    }),
    remove: useMutation({
      meta: { inline: true },
      mutationFn: api.organization.removeMember,
      onSuccess: () => {
        toast.success("Member removed from the organization");
        return refresh();
      },
    }),
    setActive: useMutation({
      meta: { inline: true },
      mutationFn: (v: { userId: string; active: boolean }) => api.organization.setMemberActive(v.userId, v.active),
      onSuccess: (member) => {
        toast.success(member.is_active ? "Member reactivated" : "Member deactivated");
        return refresh();
      },
    }),
  };
}
