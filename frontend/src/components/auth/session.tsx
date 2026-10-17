"use client";

import { createContext, useContext, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { ApiError } from "@/lib/api/errors";
import type { Me } from "@/lib/api/types";
import { useMe } from "@/lib/hooks/auth";
import { ErrorNotice } from "@/components/ui/feedback";

const SessionContext = createContext<Me | null>(null);

/** The signed-in user, organization, workspaces and capabilities. Only valid below <SessionGate>. */
export function useSession(): Me {
  const me = useContext(SessionContext);
  if (!me) throw new Error("useSession must be used inside <SessionGate>");
  return me;
}

/**
 * Renders children once /auth/me succeeds. A 401 is handled globally (redirect to /login
 * with a return URL), so while that navigation happens this just shows a spinner.
 */
export function SessionGate({ children }: { children: ReactNode }) {
  const me = useMe();
  if (me.data) return <SessionContext.Provider value={me.data}>{children}</SessionContext.Provider>;
  if (me.isError && !(me.error instanceof ApiError && me.error.status === 401)) {
    return (
      <div className="mx-auto mt-24 max-w-md px-4">
        <ErrorNotice error={me.error} title="Could not load your account" onRetry={() => me.refetch()} />
      </div>
    );
  }
  return (
    <div className="grid min-h-dvh place-items-center" role="status">
      <Loader2 aria-hidden className="size-6 animate-spin text-slate-400" />
      <span className="sr-only">Loading</span>
    </div>
  );
}
