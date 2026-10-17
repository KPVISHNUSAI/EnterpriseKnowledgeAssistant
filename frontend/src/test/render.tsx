import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { ConfirmProvider } from "@/components/ui/confirm";
import { ToastProvider } from "@/components/ui/toast";

export function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
}

export function Providers({ children, client }: { children: ReactNode; client?: QueryClient }) {
  const queryClient = client ?? makeClient();
  return (
    <ToastProvider>
      <QueryClientProvider client={queryClient}>
        <ConfirmProvider>{children}</ConfirmProvider>
      </QueryClientProvider>
    </ToastProvider>
  );
}

/** Render with the app's providers (query client, toasts, confirm dialogs). */
export function renderWithProviders(
  ui: ReactElement,
  options?: Omit<RenderOptions, "wrapper"> & { client?: QueryClient },
) {
  const { client, ...rest } = options ?? {};
  const queryClient = client ?? makeClient();
  return {
    client: queryClient,
    ...render(ui, { wrapper: ({ children }) => <Providers client={queryClient}>{children}</Providers>, ...rest }),
  };
}

export function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" }, ...init });
}

export function errorResponse(
  status: number,
  code: string,
  message: string,
  extra: { details?: unknown; requestId?: string; headers?: Record<string, string> } = {},
) {
  return jsonResponse(
    { error: { code, message, details: extra.details ?? null, request_id: extra.requestId ?? "req-test-1" } },
    { status, headers: { "content-type": "application/json", ...extra.headers } },
  );
}
