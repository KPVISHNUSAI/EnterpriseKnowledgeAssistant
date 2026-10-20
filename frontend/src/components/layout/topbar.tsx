"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Building2, Check, ChevronDown, LogOut, Menu as MenuIcon, Settings } from "lucide-react";
import { useSession } from "@/components/auth/session";
import { IconButton } from "@/components/ui/button";
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { useToast } from "@/components/ui/toast";
import { initials } from "@/lib/format";
import { useSignOut, useSwitchOrganization } from "@/lib/hooks/auth";
import { ORG_ROLE_LABELS } from "@/lib/labels";

function OrganizationSwitcher() {
  const { organization, organizations } = useSession();
  const router = useRouter();
  const toast = useToast();
  const switchOrganization = useSwitchOrganization();

  function choose(id: string, name: string) {
    if (id === organization.id) return;
    switchOrganization.mutate(id, {
      onSuccess: () => {
        toast.success(`Switched to ${name}`);
        router.replace("/dashboard");
      },
    });
  }

  return (
    <Menu>
      <MenuTrigger
        data-testid="org-switcher"
        aria-label={`Organization: ${organization.name}. Switch organization`}
        className="flex h-9 max-w-56 items-center gap-2 rounded-md border border-slate-200 bg-white px-3 text-sm font-medium text-slate-800 hover:bg-slate-50"
      >
        <Building2 aria-hidden className="size-4 shrink-0 text-slate-500" />
        <span className="truncate">{organization.name}</span>
        <ChevronDown aria-hidden className="size-4 shrink-0 text-slate-400" />
      </MenuTrigger>
      <MenuContent className="min-w-64">
        <MenuLabel>Organizations</MenuLabel>
        {organizations.map((org) => (
          <MenuItem key={org.id} onSelect={() => choose(org.id, org.name)} data-testid="org-option">
            <span className="min-w-0 flex-1">
              <span className="block truncate">{org.name}</span>
              <span className="block text-xs text-slate-500">{ORG_ROLE_LABELS[org.role]}</span>
            </span>
            {org.id === organization.id ? (
              <Check aria-label="Current organization" className="size-4 text-accent-600" />
            ) : null}
          </MenuItem>
        ))}
      </MenuContent>
    </Menu>
  );
}

function UserMenu() {
  const { user } = useSession();
  const { signOut } = useSignOut();
  return (
    <Menu>
      <MenuTrigger
        data-testid="user-menu"
        aria-label={`Account menu for ${user.full_name}`}
        className="grid size-9 place-items-center rounded-full bg-slate-800 text-xs font-semibold text-white hover:bg-slate-700"
      >
        {initials(user.full_name)}
      </MenuTrigger>
      <MenuContent>
        <MenuLabel>
          <span className="block text-sm font-medium text-slate-900">{user.full_name}</span>
          <span className="block font-normal">{user.email}</span>
        </MenuLabel>
        <MenuSeparator />
        <MenuItem asChild>
          <Link href="/settings">
            <Settings aria-hidden className="size-4 text-slate-500" /> Settings
          </Link>
        </MenuItem>
        <MenuItem onSelect={() => signOut()} data-testid="sign-out">
          <LogOut aria-hidden className="size-4 text-slate-500" /> Sign out
        </MenuItem>
      </MenuContent>
    </Menu>
  );
}

export function TopBar({ onOpenNav }: { onOpenNav: () => void }) {
  return (
    <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b border-slate-200 bg-white/95 px-4 backdrop-blur print:hidden">
      <IconButton label="Open navigation" onClick={onOpenNav} className="lg:hidden">
        <MenuIcon aria-hidden className="size-5" />
      </IconButton>
      <div className="flex-1" />
      <OrganizationSwitcher />
      <UserMenu />
    </header>
  );
}
