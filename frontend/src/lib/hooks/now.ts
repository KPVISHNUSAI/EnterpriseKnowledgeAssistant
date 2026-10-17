"use client";

import { useSyncExternalStore } from "react";

const TICK_MS = 30_000;
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | undefined;

// One shared interval, however many timestamps are on screen (tables show dozens).
function subscribe(onChange: () => void) {
  listeners.add(onChange);
  timer ??= setInterval(() => listeners.forEach((listener) => listener()), TICK_MS);
  return () => {
    listeners.delete(onChange);
    if (listeners.size === 0) {
      clearInterval(timer);
      timer = undefined;
    }
  };
}

// Rounded to the minute so the snapshot is stable between ticks.
const currentMinute = () => Math.floor(Date.now() / 60_000) * 60_000;

/** The current time (epoch ms, minute resolution), refreshed every 30 seconds; 0 while server rendering. */
export function useNow(): number {
  return useSyncExternalStore(subscribe, currentMinute, () => 0);
}
