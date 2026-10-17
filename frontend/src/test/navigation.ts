import { vi } from "vitest";

/** Controllable stand-ins for next/navigation (registered in vitest.setup.ts). */
export const router = {
  push: vi.fn(),
  replace: vi.fn(),
  back: vi.fn(),
  forward: vi.fn(),
  refresh: vi.fn(),
  prefetch: vi.fn(),
};

export const nav = {
  pathname: "/",
  search: new URLSearchParams(),
  params: {} as Record<string, string | undefined>,
};

export function resetNavigation() {
  Object.values(router).forEach((fn) => fn.mockReset());
  nav.pathname = "/";
  nav.search = new URLSearchParams();
  nav.params = {};
}
