"use client";

import { Suspense, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { SessionGate, useSession } from "@/components/auth/session";
import { Drawer } from "@/components/ui/dialog";
import { SkeletonRows } from "@/components/ui/feedback";
import { cn } from "@/lib/cn";
import { Sidebar } from "./sidebar";
import { TopBar } from "./topbar";

function Chrome({ children }: { children: ReactNode }) {
  const { capabilities } = useSession();
  const [navOpen, setNavOpen] = useState(false);
  // The chat workspace manages its own scrolling and uses the full area.
  const fullBleed = usePathname().startsWith("/chat");

  return (
    <div className="flex min-h-dvh">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[80] focus:rounded-md focus:bg-white focus:px-3 focus:py-2 focus:shadow-lg"
      >
        Skip to main content
      </a>
      <aside className="sticky top-0 hidden h-dvh w-60 shrink-0 border-r border-slate-200 bg-white lg:block">
        <Sidebar capabilities={capabilities} />
      </aside>
      <Drawer open={navOpen} onOpenChange={setNavOpen} title="Navigation" className="lg:hidden">
        <Sidebar capabilities={capabilities} onNavigate={() => setNavOpen(false)} showBrand={false} />
      </Drawer>
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar onOpenNav={() => setNavOpen(true)} />
        <main
          id="main-content"
          tabIndex={-1}
          className={cn(
            "focus:outline-hidden",
            // flex-none: the chat needs a definite height (viewport minus the top bar) to scroll internally.
            fullBleed
              ? "h-[calc(100dvh-3.5rem)] flex-none overflow-hidden"
              : "mx-auto w-full max-w-7xl flex-1 p-4 sm:p-6 lg:p-8",
          )}
        >
          <Suspense fallback={<SkeletonRows rows={5} className="p-4" />}>{children}</Suspense>
        </main>
      </div>
    </div>
  );
}

/** Authenticated application frame: sidebar (drawer on mobile), top bar, content. */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <SessionGate>
      <Chrome>{children}</Chrome>
    </SessionGate>
  );
}
