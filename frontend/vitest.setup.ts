import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { resetNavigation } from "./src/test/navigation";

vi.mock("next/navigation", async () => {
  const { nav, router } = await import("./src/test/navigation");
  return {
    useRouter: () => router,
    usePathname: () => nav.pathname,
    useSearchParams: () => nav.search,
    useParams: () => nav.params,
  };
});

afterEach(() => {
  cleanup();
  resetNavigation();
  document.cookie.split(";").forEach((cookie) => {
    document.cookie = `${cookie.split("=")[0]?.trim()}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  });
});

// jsdom lacks these browser APIs, which Radix and our components use.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub;
Element.prototype.scrollIntoView ??= () => {};
window.matchMedia ??= ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
  dispatchEvent: () => false,
})) as typeof window.matchMedia;
