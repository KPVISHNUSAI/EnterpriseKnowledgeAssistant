"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { FileText, Layers, LayoutDashboard, MessageSquare, Search, Settings, Shield, Sparkles } from "lucide-react";
import type { Capabilities } from "@/lib/api/types";
import { cn } from "@/lib/cn";

const NAV = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/documents", label: "Documents", icon: FileText },
  { href: "/chat", label: "Chat", icon: MessageSquare },
  { href: "/search", label: "Search", icon: Search },
  { href: "/workspaces", label: "Workspaces", icon: Layers },
  {
    href: "/admin",
    label: "Admin",
    icon: Shield,
    visible: (caps: Capabilities) => caps.can_manage_organization || caps.can_view_audit_log,
  },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function Sidebar({
  capabilities,
  onNavigate,
  showBrand = true,
}: {
  capabilities: Capabilities;
  onNavigate?: () => void;
  /** The mobile drawer already has a title bar. */
  showBrand?: boolean;
}) {
  const pathname = usePathname();
  return (
    <div className="flex h-full flex-col">
      {showBrand ? (
        <div className="flex h-14 shrink-0 items-center gap-2 border-b border-slate-200 px-4">
          <span className="grid size-8 place-items-center rounded-lg bg-accent-600 text-white">
            <Sparkles aria-hidden className="size-4" />
          </span>
          <span className="text-sm leading-tight font-semibold text-slate-900">
            Knowledge
            <br />
            Assistant
          </span>
        </div>
      ) : null}
      <nav aria-label="Main" className="flex-1 space-y-0.5 overflow-y-auto p-3">
        {NAV.filter((item) => !item.visible || item.visible(capabilities)).map((item) => {
          const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
          return (
            <Link
              key={item.href}
              href={item.href}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              data-testid={`nav-${item.label.toLowerCase()}`}
              className={cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                active ? "bg-accent-50 text-accent-800" : "text-slate-700 hover:bg-slate-100 hover:text-slate-900",
              )}
            >
              <item.icon aria-hidden className={cn("size-4", active ? "text-accent-700" : "text-slate-500")} />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
