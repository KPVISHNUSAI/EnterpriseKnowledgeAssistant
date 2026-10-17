"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/feedback";
import { Field, Input } from "@/components/ui/form";
import { CSRF_COOKIE, readCookie } from "@/lib/api/client";
import { api } from "@/lib/api/endpoints";
import { useMe, useSetMe } from "@/lib/hooks/auth";
import { safeNext } from "@/lib/redirect";
import { isEmail } from "@/lib/validation";

export function LoginForm() {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const setMe = useSetMe();
  // The readable CSRF cookie only exists while signed in; without it there is nothing to check (and no 401 to log).
  const existing = useMe({ enabled: readCookie(CSRF_COOKIE) !== null });
  const [errors, setErrors] = useState<{ email?: string; password?: string }>({});

  // Already signed in: go straight to where the user was headed.
  useEffect(() => {
    if (existing.data) router.replace(next);
  }, [existing.data, next, router]);

  const login = useMutation({
    mutationFn: api.auth.login,
    meta: { inline: true },
    onSuccess: (me) => {
      setMe(me);
      router.replace(next);
    },
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const email = String(form.get("email") ?? "").trim();
    const password = String(form.get("password") ?? "");
    const found: typeof errors = {};
    if (!isEmail(email)) found.email = "Enter a valid email address.";
    if (!password) found.password = "Enter your password.";
    setErrors(found);
    if (!found.email && !found.password) login.mutate({ email, password });
  }

  return (
    <>
      <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
      <p className="mt-1 mb-6 text-sm text-slate-600">Use your work account to continue.</p>
      <form onSubmit={onSubmit} noValidate className="space-y-4">
        {login.error ? <ErrorNotice error={login.error} title="Could not sign in" testId="login-error" /> : null}
        <Field label="Email" error={errors.email}>
          {(p) => (
            <Input {...p} name="email" type="email" autoComplete="email" autoFocus required data-testid="login-email" />
          )}
        </Field>
        <Field label="Password" error={errors.password}>
          {(p) => (
            <Input
              {...p}
              name="password"
              type="password"
              autoComplete="current-password"
              required
              data-testid="login-password"
            />
          )}
        </Field>
        <Button type="submit" variant="primary" className="w-full" loading={login.isPending} data-testid="login-submit">
          Sign in
        </Button>
      </form>
      <p className="mt-6 text-center text-sm text-slate-600">
        New here?{" "}
        <Link href="/register" className="font-medium text-accent-700 underline underline-offset-2">
          Create an account
        </Link>
      </p>
    </>
  );
}
