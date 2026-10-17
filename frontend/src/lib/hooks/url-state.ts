"use client";

import { usePathname, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef } from "react";

type Patch = Record<string, string | number | null | undefined>;

/** Filters, sorting and paging live in the URL so views can be shared and survive reloads. */
export function useUrlState() {
  const pathname = usePathname();
  const params = useSearchParams();

  const update = useCallback(
    (patch: Patch) => {
      const next = new URLSearchParams(params.toString());
      for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === undefined || value === "") next.delete(key);
        else next.set(key, String(value));
      }
      const query = next.toString();
      // The History API instead of router.replace: Next.js syncs useSearchParams with it, and no
      // server round trip is made for what is purely client-side state.
      window.history.replaceState(null, "", query ? `${pathname}?${query}` : pathname);
    },
    [params, pathname],
  );

  return { params, update };
}

export function intParam(value: string | null, fallback: number, min = 1): number {
  const parsed = Number.parseInt(value ?? "", 10);
  return Number.isFinite(parsed) && parsed >= min ? parsed : fallback;
}

export function enumParam<T extends string>(value: string | null, allowed: readonly T[], fallback: T): T {
  return allowed.includes(value as T) ? (value as T) : fallback;
}

/** Debounce a callback (typing in a filter box should not refetch on every keystroke). */
export function useDebouncedCallback<A extends unknown[]>(callback: (...args: A) => void, delayMs: number) {
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const latest = useRef(callback);
  useEffect(() => {
    latest.current = callback;
  });
  useEffect(() => () => clearTimeout(timer.current), []);
  return useCallback(
    (...args: A) => {
      clearTimeout(timer.current);
      timer.current = setTimeout(() => latest.current(...args), delayMs);
    },
    [delayMs],
  );
}
