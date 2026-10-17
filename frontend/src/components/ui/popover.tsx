"use client";

import * as RadixPopover from "@radix-ui/react-popover";
import type { ComponentProps } from "react";
import { cn } from "@/lib/cn";

export const Popover = RadixPopover.Root;
export const PopoverTrigger = RadixPopover.Trigger;
export const PopoverClose = RadixPopover.Close;

export function PopoverContent({ className, ...props }: ComponentProps<typeof RadixPopover.Content>) {
  return (
    <RadixPopover.Portal>
      <RadixPopover.Content
        align="start"
        sideOffset={6}
        className={cn(
          "z-50 w-[min(24rem,calc(100vw-2rem))] animate-pop-in rounded-lg border border-slate-200 bg-white p-3 shadow-lg",
          className,
        )}
        {...props}
      />
    </RadixPopover.Portal>
  );
}
