"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { ComponentProps } from "react";
import { cn } from "@/lib/cn";

export const Menu = DropdownMenu.Root;
export const MenuTrigger = DropdownMenu.Trigger;
export const MenuSeparator = () => <DropdownMenu.Separator className="my-1 h-px bg-slate-200" />;

export function MenuContent({ className, ...props }: ComponentProps<typeof DropdownMenu.Content>) {
  return (
    <DropdownMenu.Portal>
      <DropdownMenu.Content
        align="end"
        sideOffset={6}
        className={cn(
          "z-50 min-w-48 animate-pop-in rounded-lg border border-slate-200 bg-white p-1 shadow-lg",
          className,
        )}
        {...props}
      />
    </DropdownMenu.Portal>
  );
}

export function MenuLabel({ className, ...props }: ComponentProps<typeof DropdownMenu.Label>) {
  return <DropdownMenu.Label className={cn("px-2 py-1.5 text-xs font-medium text-slate-500", className)} {...props} />;
}

export function MenuItem({
  className,
  destructive,
  ...props
}: ComponentProps<typeof DropdownMenu.Item> & { destructive?: boolean }) {
  return (
    <DropdownMenu.Item
      className={cn(
        "flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-hidden select-none data-disabled:cursor-not-allowed data-disabled:opacity-50 data-highlighted:bg-slate-100",
        destructive ? "text-red-700 data-highlighted:bg-red-50" : "text-slate-800",
        className,
      )}
      {...props}
    />
  );
}
