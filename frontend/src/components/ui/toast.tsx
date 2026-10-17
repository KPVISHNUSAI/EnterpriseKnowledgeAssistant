"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";
import { errorMessage, errorRequestId } from "@/lib/api/errors";
import { cn } from "@/lib/cn";
import { IconButton } from "./button";

type Tone = "success" | "error" | "info";

interface ToastItem {
  id: number;
  tone: Tone;
  message: string;
  requestId: string | null;
}

export interface ToastApi {
  success: (message: string) => void;
  info: (message: string) => void;
  /** Accepts an ApiError/Error (message and request id are extracted) or plain text. */
  error: (error: unknown) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (!api) throw new Error("useToast must be used inside <ToastProvider>");
  return api;
}

const MAX_VISIBLE = 4;
const LIFETIME_MS: Record<Tone, number> = { success: 5000, info: 5000, error: 10000 };

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(0);

  const dismiss = useCallback((id: number) => setItems((list) => list.filter((t) => t.id !== id)), []);
  const push = useCallback((tone: Tone, message: string, requestId: string | null = null) => {
    const id = nextId.current++;
    setItems((list) => [...list.slice(-(MAX_VISIBLE - 1)), { id, tone, message, requestId }]);
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      success: (message) => push("success", message),
      info: (message) => push("info", message),
      error: (error) => push("error", typeof error === "string" ? error : errorMessage(error), errorRequestId(error)),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="pointer-events-none fixed inset-x-0 bottom-0 z-[70] flex flex-col items-end gap-2 p-4">
        {items.map((item) => (
          <ToastView key={item.id} item={item} onDismiss={dismiss} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

const icons = {
  success: <CheckCircle2 aria-hidden className="size-5 text-emerald-600" />,
  error: <AlertCircle aria-hidden className="size-5 text-red-600" />,
  info: <Info aria-hidden className="size-5 text-sky-600" />,
};

function ToastView({ item, onDismiss }: { item: ToastItem; onDismiss: (id: number) => void }) {
  useEffect(() => {
    const timer = setTimeout(() => onDismiss(item.id), LIFETIME_MS[item.tone]);
    return () => clearTimeout(timer);
  }, [item.id, item.tone, onDismiss]);

  return (
    <div
      role={item.tone === "error" ? "alert" : "status"}
      data-testid={`toast-${item.tone}`}
      className={cn(
        "pointer-events-auto flex w-full max-w-sm animate-toast-in items-start gap-3 rounded-lg border bg-white p-3 shadow-lg",
        item.tone === "error" ? "border-red-200" : "border-slate-200",
      )}
    >
      <span className="mt-0.5 shrink-0">{icons[item.tone]}</span>
      <div className="min-w-0 flex-1 text-sm text-slate-800">
        <p>{item.message}</p>
        {item.requestId ? (
          <p className="mt-1 text-xs text-slate-500">
            Request ID: <code className="font-mono select-all">{item.requestId}</code>
          </p>
        ) : null}
      </div>
      <IconButton label="Dismiss notification" className="-mr-1 -mt-1 size-6" onClick={() => onDismiss(item.id)}>
        <X aria-hidden className="size-4" />
      </IconButton>
    </div>
  );
}
