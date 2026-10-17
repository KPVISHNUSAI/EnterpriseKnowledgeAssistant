"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useSession } from "@/components/auth/session";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardHeader, ErrorNotice, PageHeader } from "@/components/ui/feedback";
import { Field, Input } from "@/components/ui/form";
import { useToast } from "@/components/ui/toast";
import { api } from "@/lib/api/endpoints";
import { fieldErrors } from "@/lib/api/errors";
import { useSignOut, useSwitchOrganization } from "@/lib/hooks/auth";
import { keys } from "@/lib/hooks/keys";
import { ORG_ROLE_LABELS } from "@/lib/labels";
import { PASSWORD_MAX_LENGTH, passwordError } from "@/lib/validation";

function ProfileCard() {
  const { user } = useSession();
  const client = useQueryClient();
  const toast = useToast();
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: api.auth.updateProfile,
    meta: { inline: true },
    onSuccess: async () => {
      toast.success("Profile updated");
      await client.invalidateQueries({ queryKey: keys.me });
    },
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = String(new FormData(event.currentTarget).get("full_name") ?? "").trim();
    if (!name) return setError("Enter your name.");
    setError(null);
    save.mutate(name);
  }

  return (
    <Card>
      <CardHeader title="Profile" />
      <form onSubmit={onSubmit} noValidate className="max-w-md space-y-4 p-4">
        {save.error && !fieldErrors(save.error).full_name ? (
          <ErrorNotice error={save.error} title="Could not update the profile" />
        ) : null}
        <Field label="Full name" error={error ?? fieldErrors(save.error).full_name}>
          {(p) => (
            <Input
              {...p}
              name="full_name"
              defaultValue={user.full_name}
              maxLength={200}
              autoComplete="name"
              required
              data-testid="profile-name"
            />
          )}
        </Field>
        <Field label="Email" hint="Your email is your sign-in name and cannot be changed here.">
          {(p) => <Input {...p} value={user.email} readOnly disabled />}
        </Field>
        <Button type="submit" variant="primary" loading={save.isPending} data-testid="profile-save">
          Save profile
        </Button>
      </form>
    </Card>
  );
}

function PasswordCard() {
  const toast = useToast();
  const [errors, setErrors] = useState<{ current?: string; next?: string; confirm?: string }>({});
  const [formKey, setFormKey] = useState(0);
  const change = useMutation({
    mutationFn: api.auth.changePassword,
    meta: { inline: true },
    onSuccess: () => {
      toast.success("Password changed. Your other sessions were signed out.");
      setFormKey((key) => key + 1); // clears the fields
    },
  });
  const server = fieldErrors(change.error);

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const current = String(form.get("current_password") ?? "");
    const next = String(form.get("new_password") ?? "");
    const confirm = String(form.get("confirm_password") ?? "");
    const found: typeof errors = {};
    if (!current) found.current = "Enter your current password.";
    const weak = passwordError(next);
    if (weak) found.next = weak;
    else if (next === current) found.next = "Choose a password different from the current one.";
    if (next !== confirm) found.confirm = "The passwords do not match.";
    setErrors(found);
    if (Object.keys(found).length === 0) change.mutate({ current_password: current, new_password: next });
  }

  return (
    <Card>
      <CardHeader title="Password" description="Changing it signs you out everywhere else." />
      <form key={formKey} onSubmit={onSubmit} noValidate className="max-w-md space-y-4 p-4">
        {change.error && Object.keys(server).length === 0 ? (
          <ErrorNotice error={change.error} title="Could not change the password" testId="password-error" />
        ) : null}
        <Field label="Current password" error={errors.current ?? server.current_password}>
          {(p) => (
            <Input
              {...p}
              type="password"
              name="current_password"
              autoComplete="current-password"
              required
              data-testid="current-password"
            />
          )}
        </Field>
        <Field label="New password" hint="At least 12 characters." error={errors.next ?? server.new_password}>
          {(p) => (
            <Input
              {...p}
              type="password"
              name="new_password"
              autoComplete="new-password"
              maxLength={PASSWORD_MAX_LENGTH}
              required
              data-testid="new-password"
            />
          )}
        </Field>
        <Field label="Confirm new password" error={errors.confirm}>
          {(p) => (
            <Input
              {...p}
              type="password"
              name="confirm_password"
              autoComplete="new-password"
              maxLength={PASSWORD_MAX_LENGTH}
              required
              data-testid="confirm-password"
            />
          )}
        </Field>
        <Button type="submit" variant="primary" loading={change.isPending} data-testid="password-save">
          Change password
        </Button>
      </form>
    </Card>
  );
}

function OrganizationsCard() {
  const { organization, organizations } = useSession();
  const router = useRouter();
  const toast = useToast();
  const switchOrganization = useSwitchOrganization();

  return (
    <Card>
      <CardHeader
        title="Organizations"
        description="Your documents, chats and permissions are separate in each organization."
      />
      <ul className="divide-y divide-slate-100">
        {organizations.map((org) => {
          const current = org.id === organization.id;
          return (
            <li key={org.id} className="flex items-center justify-between gap-3 px-4 py-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-slate-900">{org.name}</p>
                <p className="text-xs text-slate-500">{ORG_ROLE_LABELS[org.role]}</p>
              </div>
              {current ? (
                <Badge tone="accent">Current</Badge>
              ) : (
                <Button
                  size="sm"
                  loading={switchOrganization.isPending && switchOrganization.variables === org.id}
                  onClick={() =>
                    switchOrganization.mutate(org.id, {
                      onSuccess: () => {
                        toast.success(`Switched to ${org.name}`);
                        router.replace("/dashboard");
                      },
                    })
                  }
                >
                  Switch
                </Button>
              )}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

function SessionCard() {
  const { signOut, pending } = useSignOut();
  return (
    <Card>
      <CardHeader title="Session" />
      <div className="p-4">
        <Button onClick={() => signOut()} loading={pending} data-testid="settings-sign-out">
          Sign out
        </Button>
      </div>
    </Card>
  );
}

export default function SettingsPage() {
  return (
    <>
      <PageHeader title="Settings" description="Your profile, password and organizations." />
      <div className="max-w-3xl space-y-6">
        <ProfileCard />
        <PasswordCard />
        <OrganizationsCard />
        <SessionCard />
      </div>
    </>
  );
}
