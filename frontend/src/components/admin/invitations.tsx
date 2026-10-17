"use client";

import { useState, type FormEvent } from "react";
import { Link2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { CopyButton } from "@/components/ui/copy-button";
import { Modal } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, ErrorNotice, SkeletonRows } from "@/components/ui/feedback";
import { Field, Input, Select } from "@/components/ui/form";
import { Pagination } from "@/components/ui/pagination";
import { Time } from "@/components/ui/time";
import { fieldErrors } from "@/lib/api/errors";
import type { Invitation, InvitationCreated, OrgRole } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { useCreateInvitation, useInvitations, useRevokeInvitation } from "@/lib/hooks/admin";
import { useNow } from "@/lib/hooks/now";
import { ORG_ROLES, ORG_ROLE_LABELS } from "@/lib/labels";
import { isEmail } from "@/lib/validation";

function invitationStatus(invitation: Invitation, now: number): "accepted" | "revoked" | "expired" | "pending" {
  if (invitation.accepted_at) return "accepted";
  if (invitation.revoked_at) return "revoked";
  if (now > 0 && new Date(invitation.expires_at).getTime() < now) return "expired";
  return "pending";
}

const STATUS_TONES = { accepted: "success", revoked: "neutral", expired: "warning", pending: "info" } as const;

function InviteDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const create = useCreateInvitation();
  const [emailError, setEmailError] = useState<string | null>(null);
  const [created, setCreated] = useState<InvitationCreated | null>(null);
  const server = fieldErrors(create.error);

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      setCreated(null);
      setEmailError(null);
      create.reset();
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const email = String(form.get("email") ?? "").trim();
    if (!isEmail(email)) return setEmailError("Enter a valid email address.");
    setEmailError(null);
    create.mutate({ email, role: form.get("role") as OrgRole }, { onSuccess: setCreated });
  }

  if (created) {
    return (
      <Modal
        open={open}
        onOpenChange={close}
        title="Invitation created"
        description={`${created.email} can join as ${ORG_ROLE_LABELS[created.role]}.`}
        footer={
          <Button variant="primary" onClick={() => close(false)}>
            Done
          </Button>
        }
      >
        <div className="space-y-3">
          <Alert tone="warning" title="Copy the link now">
            For security this link is shown only once. Anyone who has it can join using {created.email}. It expires{" "}
            {formatDateTime(created.expires_at)}.
          </Alert>
          <label htmlFor="invite-link" className="block text-sm font-medium text-slate-700">
            Invite link
          </label>
          <div className="flex gap-2">
            <Input
              id="invite-link"
              readOnly
              value={created.invite_url}
              onFocus={(e) => e.currentTarget.select()}
              data-testid="invite-link"
            />
            <CopyButton text={created.invite_url} label="Copy link" variant="secondary" />
          </div>
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      open={open}
      onOpenChange={close}
      title="Invite someone"
      description="They will receive a single-use link to join this organization."
    >
      <form onSubmit={onSubmit} noValidate className="space-y-4">
        {create.error && Object.keys(server).length === 0 ? (
          <ErrorNotice error={create.error} title="Could not create the invitation" />
        ) : null}
        <Field label="Email address" error={emailError ?? server.email}>
          {(p) => (
            <Input {...p} name="email" type="email" autoComplete="off" autoFocus required data-testid="invite-email" />
          )}
        </Field>
        <Field label="Role" hint="Organization admins can manage members, invitations, usage and the audit log.">
          {(p) => (
            <Select {...p} name="role" defaultValue="employee" data-testid="invite-role">
              {ORG_ROLES.map((role) => (
                <option key={role} value={role}>
                  {ORG_ROLE_LABELS[role]}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <div className="flex justify-end gap-2">
          <Button onClick={() => close(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={create.isPending} data-testid="invite-submit">
            Create invitation
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function Invitations() {
  const confirm = useConfirm();
  const now = useNow();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [inviting, setInviting] = useState(false);
  const invitations = useInvitations({ page, page_size: pageSize });
  const revoke = useRevokeInvitation();

  async function onRevoke(invitation: Invitation) {
    const ok = await confirm({
      title: "Revoke this invitation?",
      description: `The link sent to ${invitation.email} will stop working.`,
      confirmLabel: "Revoke invitation",
      destructive: true,
    });
    if (ok) revoke.mutate(invitation.id);
  }

  return (
    <div>
      <div className="mb-4 flex justify-end">
        <Button variant="primary" onClick={() => setInviting(true)} data-testid="invite-open">
          <Plus aria-hidden className="size-4" /> Invite someone
        </Button>
      </div>
      {invitations.isError ? (
        <ErrorNotice
          error={invitations.error}
          title="Could not load invitations"
          onRetry={() => invitations.refetch()}
        />
      ) : invitations.isPending ? (
        <SkeletonRows rows={4} />
      ) : invitations.data.items.length === 0 ? (
        <EmptyState
          icon={<Link2 />}
          title="No invitations yet"
          description="Invite colleagues by email. Each invitation is a single-use link."
        />
      ) : (
        <div className="space-y-4">
          <div className="relative overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="w-full min-w-[40rem] text-left text-sm" data-testid="invitations-table">
              <caption className="sr-only">Invitations</caption>
              <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-600">
                <tr>
                  <th scope="col" className="px-4 py-2 font-medium">
                    Email
                  </th>
                  <th scope="col" className="px-4 py-2 font-medium">
                    Role
                  </th>
                  <th scope="col" className="px-4 py-2 font-medium">
                    Status
                  </th>
                  <th scope="col" className="hidden px-4 py-2 font-medium md:table-cell">
                    Invited by
                  </th>
                  <th scope="col" className="hidden px-4 py-2 font-medium sm:table-cell">
                    Expires
                  </th>
                  <th scope="col" className="px-4 py-2">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {invitations.data.items.map((invitation) => {
                  const status = invitationStatus(invitation, now);
                  return (
                    <tr key={invitation.id} data-testid="invitation-row" data-status={status}>
                      <td className="px-4 py-3 text-slate-900">{invitation.email}</td>
                      <td className="px-4 py-3 text-slate-700">{ORG_ROLE_LABELS[invitation.role]}</td>
                      <td className="px-4 py-3">
                        <Badge tone={STATUS_TONES[status]}>{status[0]?.toUpperCase() + status.slice(1)}</Badge>
                      </td>
                      <td className="hidden px-4 py-3 text-slate-600 md:table-cell">
                        {invitation.invited_by_email ?? "-"}
                      </td>
                      <td className="hidden px-4 py-3 text-slate-600 sm:table-cell">
                        <Time iso={invitation.expires_at} absolute />
                      </td>
                      <td className="px-4 py-3 text-right">
                        {status === "pending" ? (
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() => onRevoke(invitation)}
                            aria-label={`Revoke invitation for ${invitation.email}`}
                          >
                            Revoke
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pagination
            page={invitations.data.page}
            pageSize={invitations.data.page_size}
            total={invitations.data.total}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setPage(1);
            }}
          />
        </div>
      )}
      <InviteDialog open={inviting} onOpenChange={setInviting} />
    </div>
  );
}
