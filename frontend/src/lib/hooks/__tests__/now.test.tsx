import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useNow } from "../now";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-18T12:00:20Z"));
});
afterEach(() => vi.useRealTimers());

describe("useNow", () => {
  it("reports the current minute and moves on with the clock", () => {
    const { result } = renderHook(() => useNow());
    expect(result.current).toBe(Date.parse("2026-10-18T12:00:00Z"));

    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(result.current).toBe(Date.parse("2026-10-18T12:01:00Z"));
  });

  it("shares a single interval between components and stops it when none are left", () => {
    const setSpy = vi.spyOn(globalThis, "setInterval");
    const clearSpy = vi.spyOn(globalThis, "clearInterval");
    const first = renderHook(() => useNow());
    const second = renderHook(() => useNow());
    expect(setSpy).toHaveBeenCalledTimes(1);

    first.unmount();
    expect(clearSpy).not.toHaveBeenCalled();
    second.unmount();
    expect(clearSpy).toHaveBeenCalledTimes(1);
  });
});
