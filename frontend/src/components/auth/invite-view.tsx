"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Button, LinkButton } from "@/components/ui/button";
import { Alert, ErrorNotice, Spinner } from "@/components/ui/feedback";
import { api } from "@/lib/api/endpoints";
import { ApiError } from "@/lib/api/errors";
import { formatDateTime } from "@/lib/format";
import { useMe, useSetMe, useSignOut } from "@/lib/hooks/auth";
import { ORG_ROLE_LABELS } from "@/lib/labels";
import { loginUrl } from "@/lib/redirect";
import { RegisterForm } from "./register-form";

/** Landing page of an invitation link: sign up (logged out) or accept (logged in). */
export function InviteView({ token }: { token: string }) {
  const router = useRouter();
  const setMe = useSetMe();
  const { signOut, pending: signingOut } = useSignOut();
  const me = useMe();
  const preview = useQuery({
    queryKey: ["invitation", token],
    queryFn: () => api.auth.previewInvitation(token),
    retry: false,
  });
  const accept = useMutation({
    mutationFn: () => api.auth.acceptInvitation(token),
    meta: { inline: true },
    onSuccess: (updated) => {
      setMe(updated);
      router.replace("/dashboard");
    },
  });

  if (preview.isPending || me.isPending) {
    return (
      <div className="flex justify-center py-8">
        <Spinner className="size-6" />
      </div>
    );
  }

  if (preview.isError) {
    const gone = preview.error instanceof ApiError && preview.error.status === 404;
    return (
      <>
        <h1 className="text-xl font-semibold tracking-tight">Invitation unavailable</h1>
        <div className="mt-4">
          {gone ? (
            <Alert tone="warning">
              This invitation is invalid, has expired, was revoked or has already been used. Ask your administrator for
              a new one.
            </Alert>
          ) : (
            <ErrorNotice error={preview.error} onRetry={() => preview.refetch()} />
          )}
        </div>
        <LinkButton href="/login" className="mt-6 w-full">
          Go to sign in
        </LinkButton>
      </>
    );
  }

  const invitation = preview.data;
  const signedIn = me.data;

  if (!signedIn) {
    return <RegisterForm invite={{ token, email: invitation.email, organizationName: invitation.organization_name }} />;
  }

  const matches = signedIn.user.email.toLowerCase() === invitation.email.toLowerCase();
  return (
    <>
      <h1 className="text-xl font-semibold tracking-tight">Join {invitation.organization_name}</h1>
      <p className="mt-1 text-sm text-slate-600">
        You were invited as <strong>{ORG_ROLE_LABELS[invitation.role]}</strong>. This invitation is for{" "}
        <strong>{invitation.email}</strong> and expires {formatDateTime(invitation.expires_at)}.
      </p>
      <div className="mt-5 space-y-4">
        {accept.error ? <ErrorNotice error={accept.error} title="Could not accept the invitation" /> : null}
        {matches ? (
          <Button variant="primary" className="w-full" loading={accept.isPending} onClick={() => accept.mutate()}>
            Accept invitation
          </Button>
        ) : (
          <>
            <Alert tone="warning">
              You are signed in as {signedIn.user.email}. Sign in with {invitation.email} to accept this invitation.
            </Alert>
            <Button className="w-full" loading={signingOut} onClick={() => signOut(loginUrl(`/invite/${token}`))}>
              Sign out and switch account
            </Button>
          </>
        )}
        <p className="text-center text-sm">
          <Link href="/dashboard" className="text-slate-600 underline underline-offset-2">
            Not now
          </Link>
        </p>
      </div>
    </>
  );
}
