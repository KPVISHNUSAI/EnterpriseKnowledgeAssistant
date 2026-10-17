"use client";

import { useState } from "react";
import { Trash2, UserPlus } from "lucide-react";
import { UserPicker } from "@/components/shared/user-picker";
import { Button, IconButton } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { Card, CardHeader, ErrorNotice, SkeletonRows } from "@/components/ui/feedback";
import { Select } from "@/components/ui/form";
import { Time } from "@/components/ui/time";
import type { OrgMember, WorkspaceMember, WorkspaceRole } from "@/lib/api/types";
import { initials } from "@/lib/format";
import { useWorkspaceMemberActions, useWorkspaceMembers } from "@/lib/hooks/workspaces";
import { WORKSPACE_ROLES, WORKSPACE_ROLE_LABELS } from "@/lib/labels";
import { useSession } from "@/components/auth/session";

type Actions = ReturnType<typeof useWorkspaceMemberActions>;

function AddMember({ add, existing }: { add: Actions["add"]; existing: ReadonlySet<string> }) {
  const [person, setPerson] = useState<OrgMember | null>(null);
  const [role, setRole] = useState<WorkspaceRole>("viewer");

  return (
    <div className="border-b border-slate-200 bg-slate-50/60 p-4">
      <p className="mb-2 text-sm font-medium text-slate-800">Add a member</p>
      <div className="flex flex-wrap items-center gap-2">
        <UserPicker exclude={existing} onSelect={setPerson}>
          <Button data-testid="pick-member">
            <UserPlus aria-hidden className="size-4" />
            {person ? person.full_name : "Choose a person"}
          </Button>
        </UserPicker>
        <Select
          aria-label="Role for the new member"
          className="w-32"
          value={role}
          onChange={(e) => setRole(e.target.value as WorkspaceRole)}
        >
          {WORKSPACE_ROLES.map((r) => (
            <option key={r} value={r}>
              {WORKSPACE_ROLE_LABELS[r]}
            </option>
          ))}
        </Select>
        <Button
          variant="primary"
          disabled={!person}
          loading={add.isPending}
          data-testid="add-member"
          onClick={() => person && add.mutate({ user_id: person.user_id, role }, { onSuccess: () => setPerson(null) })}
        >
          Add
        </Button>
      </div>
      {add.error ? <ErrorNotice className="mt-3" error={add.error} title="Could not add the member" /> : null}
    </div>
  );
}

function MemberRow({ member, actions, isSelf }: { member: WorkspaceMember; actions: Actions; isSelf: boolean }) {
  const confirm = useConfirm();
  const { changeRole, remove } = actions;

  async function onRemove() {
    const ok = await confirm({
      title: `Remove ${member.full_name}?`,
      description: "They will lose access to this workspace and the documents in it.",
      confirmLabel: "Remove member",
      destructive: true,
    });
    if (ok) remove.mutate(member.user_id);
  }

  return (
    <tr data-testid="member-row">
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
              {member.full_name} {isSelf ? <span className="font-normal text-slate-500">(you)</span> : null}
            </p>
            <p className="truncate text-xs text-slate-500">{member.email}</p>
          </div>
        </div>
      </td>
      <td className="px-4 py-3">
        <Select
          aria-label={`Role of ${member.full_name}`}
          className="h-8 w-32 text-[13px]"
          value={member.role}
          disabled={changeRole.isPending}
          onChange={(event) => changeRole.mutate({ userId: member.user_id, role: event.target.value as WorkspaceRole })}
        >
          {WORKSPACE_ROLES.map((role) => (
            <option key={role} value={role}>
              {WORKSPACE_ROLE_LABELS[role]}
            </option>
          ))}
        </Select>
      </td>
      <td className="hidden px-4 py-3 text-sm text-slate-600 sm:table-cell">
        <Time iso={member.added_at} />
      </td>
      <td className="px-4 py-3 text-right">
        <IconButton label={`Remove ${member.full_name}`} onClick={onRemove} disabled={remove.isPending}>
          <Trash2 aria-hidden className="size-4" />
        </IconButton>
      </td>
    </tr>
  );
}

export function WorkspaceMembers({ workspaceId, canManage }: { workspaceId: string; canManage: boolean }) {
  const { user } = useSession();
  const members = useWorkspaceMembers(workspaceId);
  const actions = useWorkspaceMemberActions(workspaceId);
  const actionError = actions.changeRole.error ?? actions.remove.error;

  return (
    <Card>
      <CardHeader
        title="Members"
        description="Owners manage members and every document; editors upload; viewers read."
      />
      {canManage && members.data ? (
        <AddMember add={actions.add} existing={new Set(members.data.map((m) => m.user_id))} />
      ) : null}
      {actionError ? (
        <ErrorNotice className="m-4" error={actionError} title="Could not update the members" testId="member-error" />
      ) : null}
      {members.isError ? (
        <div className="p-4">
          <ErrorNotice error={members.error} onRetry={() => members.refetch()} />
        </div>
      ) : members.isPending ? (
        <SkeletonRows className="p-4" rows={3} />
      ) : canManage ? (
        <div className="relative overflow-x-auto">
          <table className="w-full text-left" data-testid="members-table">
            <caption className="sr-only">Workspace members</caption>
            <thead className="border-b border-slate-200 text-xs text-slate-500">
              <tr>
                <th scope="col" className="px-4 py-2 font-medium">
                  Person
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Role
                </th>
                <th scope="col" className="hidden px-4 py-2 font-medium sm:table-cell">
                  Added
                </th>
                <th scope="col" className="px-4 py-2">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {members.data.map((member) => (
                <MemberRow key={member.user_id} member={member} actions={actions} isSelf={member.user_id === user.id} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <ul className="divide-y divide-slate-100" data-testid="members-table">
          {members.data.map((member) => (
            <li key={member.user_id} className="flex items-center justify-between gap-3 px-4 py-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-slate-900">{member.full_name}</p>
                <p className="truncate text-xs text-slate-500">{member.email}</p>
              </div>
              <span className="text-sm text-slate-600">{WORKSPACE_ROLE_LABELS[member.role]}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
