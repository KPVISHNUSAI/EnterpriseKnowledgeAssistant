"use client";

import { useState } from "react";
import { Trash2, UserCheck, UserX } from "lucide-react";
import { useSession } from "@/components/auth/session";
import { IconButton } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { Badge, EmptyState, ErrorNotice, SkeletonRows } from "@/components/ui/feedback";
import { Field, Input, Select } from "@/components/ui/form";
import { Pagination } from "@/components/ui/pagination";
import { Time } from "@/components/ui/time";
import type { OrgRole } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { initials } from "@/lib/format";
import { useOrgMemberActions } from "@/lib/hooks/admin";
import { useDebouncedCallback } from "@/lib/hooks/url-state";
import { useOrgMembers } from "@/lib/hooks/workspaces";
import { ORG_ROLES, ORG_ROLE_LABELS } from "@/lib/labels";

export function OrgMembers() {
  const { user } = useSession();
  const confirm = useConfirm();
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const members = useOrgMembers({ q: q || undefined, page, page_size: pageSize });
  const { changeRole, remove, setActive } = useOrgMemberActions();
  const onSearch = useDebouncedCallback((value: string) => {
    setQ(value);
    setPage(1);
  }, 300);
  const actionError = changeRole.error ?? remove.error ?? setActive.error;
  // One change at a time, so two actions on the same member cannot overlap.
  const busy = changeRole.isPending || remove.isPending || setActive.isPending;

  async function onRemove(userId: string, name: string) {
    const ok = await confirm({
      title: `Remove ${name} from the organization?`,
      description:
        "They lose access to every workspace and document here, and their conversations in this organization are deleted.",
      confirmLabel: "Remove member",
      destructive: true,
    });
    if (ok) remove.mutate(userId);
  }

  async function onDeactivate(userId: string, name: string) {
    const ok = await confirm({
      title: `Deactivate ${name}?`,
      description:
        "They are signed out everywhere and cannot sign in until reactivated. Their workspaces, document access and conversations are kept.",
      confirmLabel: "Deactivate",
      destructive: true,
    });
    if (ok) setActive.mutate({ userId, active: false });
  }

  return (
    <div>
      <Field label="Search members" hideLabel className="mb-4 max-w-sm">
        {(p) => (
          <Input
            {...p}
            type="search"
            placeholder="Search by name or email"
            onChange={(e) => onSearch(e.target.value.trim())}
          />
        )}
      </Field>
      {actionError ? (
        <ErrorNotice
          className="mb-4"
          error={actionError}
          title="Could not update the member"
          testId="org-member-error"
        />
      ) : null}
      {members.isError ? (
        <ErrorNotice error={members.error} title="Could not load members" onRetry={() => members.refetch()} />
      ) : members.isPending ? (
        <SkeletonRows rows={5} />
      ) : members.data.items.length === 0 ? (
        <EmptyState title="No members found" description={q ? "Try a different search." : undefined} />
      ) : (
        <div className="space-y-4">
          <div
            className={cn(
              "relative overflow-x-auto rounded-lg border border-slate-200 bg-white",
              members.isPlaceholderData && "opacity-60",
            )}
          >
            <table className="w-full min-w-[36rem] text-left" data-testid="org-members-table">
              <caption className="sr-only">Organization members</caption>
              <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-600">
                <tr>
                  <th scope="col" className="px-4 py-2 font-medium">
                    Person
                  </th>
                  <th scope="col" className="px-4 py-2 font-medium">
                    Role
                  </th>
                  <th scope="col" className="hidden px-4 py-2 font-medium sm:table-cell">
                    Joined
                  </th>
                  <th scope="col" className="px-4 py-2">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {members.data.items.map((member) => (
                  <tr key={member.user_id} data-testid="org-member-row">
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-3">
                        <span
                          aria-hidden
                          className="grid size-8 shrink-0 place-items-center rounded-full bg-slate-100 text-xs font-semibold text-slate-600"
                        >
                          {initials(member.full_name)}
                        </span>
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium text-slate-900">
                            {member.full_name}{" "}
                            {member.user_id === user.id ? (
                              <span className="font-normal text-slate-500">(you)</span>
                            ) : null}
                            {member.is_active ? null : <Badge tone="warning">Deactivated</Badge>}
                          </p>
                          <p className="truncate text-xs text-slate-500">{member.email}</p>
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <Select
                        aria-label={`Role of ${member.full_name}`}
                        className="h-8 w-48 text-[13px]"
                        value={member.role}
                        disabled={busy}
                        onChange={(e) => changeRole.mutate({ userId: member.user_id, role: e.target.value as OrgRole })}
                      >
                        {ORG_ROLES.map((role) => (
                          <option key={role} value={role}>
                            {ORG_ROLE_LABELS[role]}
                          </option>
                        ))}
                      </Select>
                    </td>
                    <td className="hidden px-4 py-3 text-sm text-slate-600 sm:table-cell">
                      <Time iso={member.joined_at} />
                    </td>
                    <td className="px-4 py-3 text-right whitespace-nowrap">
                      {member.user_id === user.id ? null : member.is_active ? (
                        <IconButton
                          label={`Deactivate ${member.full_name}`}
                          disabled={busy}
                          onClick={() => onDeactivate(member.user_id, member.full_name)}
                        >
                          <UserX aria-hidden className="size-4" />
                        </IconButton>
                      ) : (
                        <IconButton
                          label={`Reactivate ${member.full_name}`}
                          disabled={busy}
                          onClick={() => setActive.mutate({ userId: member.user_id, active: true })}
                        >
                          <UserCheck aria-hidden className="size-4" />
                        </IconButton>
                      )}
                      <IconButton
                        label={`Remove ${member.full_name}`}
                        disabled={busy}
                        onClick={() => onRemove(member.user_id, member.full_name)}
                      >
                        <Trash2 aria-hidden className="size-4" />
                      </IconButton>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination
            page={members.data.page}
            pageSize={members.data.page_size}
            total={members.data.total}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setPage(1);
            }}
          />
        </div>
      )}
    </div>
  );
}
