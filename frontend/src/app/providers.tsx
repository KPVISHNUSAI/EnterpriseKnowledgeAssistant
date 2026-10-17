"use client";

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { ApiError, isAbortError } from "@/lib/api/errors";
import { loginUrl } from "@/lib/redirect";
import { ConfirmProvider } from "@/components/ui/confirm";
import { ToastProvider, useToast, type ToastApi } from "@/components/ui/toast";

declare module "@tanstack/react-query" {
  interface Register {
    mutationMeta: {
      /** The caller shows this mutation's error itself, so no toast. */
      inline?: boolean;
    };
  }
}

const PUBLIC_PREFIXES = ["/login", "/register", "/invite"];

/** Session expired or never existed: go to the sign-in page and come back afterwards. */
function redirectToLogin() {
  const { pathname, search } = window.location;
  if (PUBLIC_PREFIXES.some((prefix) => pathname.startsWith(prefix))) return;
  window.location.assign(loginUrl(pathname + search));
}

function isUnauthorized(error: unknown) {
  return error instanceof ApiError && error.status === 401;
}

function makeQueryClient(toast: ToastApi) {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        if (isUnauthorized(error)) redirectToLogin();
      },
    }),
    mutationCache: new MutationCache({
      onError: (error, _vars, _ctx, mutation) => {
        if (isAbortError(error)) return;
        if (isUnauthorized(error) && !mutation.meta?.inline) return redirectToLogin();
        if (!mutation.meta?.inline) toast.error(error);
      },
    }),
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        // Client errors (4xx) will not fix themselves; retry only transient failures.
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      },
    },
  });
}

function QueryProvider({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [client] = useState(() => makeQueryClient(toast));
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

export function Providers({ children }: { children: ReactNode }) {
  return (
    <ToastProvider>
      <QueryProvider>
        <ConfirmProvider>{children}</ConfirmProvider>
      </QueryProvider>
    </ToastProvider>
  );
}
