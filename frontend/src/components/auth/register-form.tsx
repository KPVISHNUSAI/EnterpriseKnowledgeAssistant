"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/feedback";
import { Checkbox, Field, Input } from "@/components/ui/form";
import { api } from "@/lib/api/endpoints";
import { fieldErrors } from "@/lib/api/errors";
import { useSetMe } from "@/lib/hooks/auth";
import { PASSWORD_MAX_LENGTH, isEmail, passwordError } from "@/lib/validation";

export interface InviteContext {
  token: string;
  email: string;
  organizationName: string;
}

type Errors = Partial<Record<"full_name" | "email" | "password" | "organization_name", string>>;

/** Account creation: a new organization, or joining one through an invitation. */
export function RegisterForm({ invite }: { invite?: InviteContext }) {
  const router = useRouter();
  const setMe = useSetMe();
  const [clientErrors, setClientErrors] = useState<Errors>({});
  const [showPassword, setShowPassword] = useState(false);

  const register = useMutation({
    mutationFn: api.auth.register,
    meta: { inline: true },
    onSuccess: (me) => {
      setMe(me);
      router.replace("/dashboard");
    },
  });
  const errors: Errors = { ...fieldErrors(register.error), ...clientErrors };

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const value = (name: string) => String(form.get(name) ?? "").trim();
    const password = String(form.get("password") ?? "");
    const found: Errors = {};
    if (!value("full_name")) found.full_name = "Enter your name.";
    if (!invite && !isEmail(value("email"))) found.email = "Enter a valid email address.";
    const weak = passwordError(password);
    if (weak) found.password = weak;
    if (!invite && !value("organization_name")) found.organization_name = "Enter a name for your organization.";
    setClientErrors(found);
    if (Object.keys(found).length > 0) return;
    register.mutate({
      full_name: value("full_name"),
      email: invite ? invite.email : value("email"),
      password,
      ...(invite ? { invite_token: invite.token } : { organization_name: value("organization_name") }),
    });
  }

  return (
    <>
      <h1 className="text-xl font-semibold tracking-tight">
        {invite ? `Join ${invite.organizationName}` : "Create your account"}
      </h1>
      <p className="mt-1 mb-6 text-sm text-slate-600">
        {invite
          ? "Create your account to accept the invitation."
          : "You will be the administrator of the new organization."}
      </p>
      <form onSubmit={onSubmit} noValidate className="space-y-4">
        {register.error && Object.keys(fieldErrors(register.error)).length === 0 ? (
          <ErrorNotice error={register.error} title="Could not create the account" testId="register-error" />
        ) : null}
        <Field label="Full name" error={errors.full_name}>
          {(p) => <Input {...p} name="full_name" autoComplete="name" required data-testid="register-name" />}
        </Field>
        <Field label="Work email" error={errors.email} hint={invite ? "Set by the invitation." : undefined}>
          {(p) => (
            <Input
              {...p}
              name="email"
              type="email"
              autoComplete="email"
              required
              readOnly={Boolean(invite)}
              defaultValue={invite?.email}
              data-testid="register-email"
            />
          )}
        </Field>
        <Field label="Password" error={errors.password} hint="At least 12 characters.">
          {(p) => (
            <Input
              {...p}
              name="password"
              type={showPassword ? "text" : "password"}
              autoComplete="new-password"
              maxLength={PASSWORD_MAX_LENGTH}
              required
              data-testid="register-password"
            />
          )}
        </Field>
        <label className="flex items-center gap-2 text-sm text-slate-600">
          <Checkbox checked={showPassword} onChange={(event) => setShowPassword(event.target.checked)} />
          Show password
        </label>
        {invite ? null : (
          <Field label="Organization name" error={errors.organization_name}>
            {(p) => (
              <Input {...p} name="organization_name" autoComplete="organization" required data-testid="register-org" />
            )}
          </Field>
        )}
        <Button
          type="submit"
          variant="primary"
          className="w-full"
          loading={register.isPending}
          data-testid="register-submit"
        >
          {invite ? "Create account and join" : "Create account"}
        </Button>
      </form>
      <p className="mt-6 text-center text-sm text-slate-600">
        Already have an account?{" "}
        <Link
          href={invite ? `/login?next=${encodeURIComponent(`/invite/${invite.token}`)}` : "/login"}
          className="font-medium text-accent-700 underline underline-offset-2"
        >
          Sign in
        </Link>
      </p>
    </>
  );
}
