"use client";

import * as RadixTabs from "@radix-ui/react-tabs";
import type { ComponentProps } from "react";
import { cn } from "@/lib/cn";

export const Tabs = RadixTabs.Root;

export function TabList({ className, ...props }: ComponentProps<typeof RadixTabs.List>) {
  return (
    <RadixTabs.List
      className={cn("-mb-px flex gap-1 overflow-x-auto border-b border-slate-200", className)}
      {...props}
    />
  );
}

export function Tab({ className, ...props }: ComponentProps<typeof RadixTabs.Trigger>) {
  return (
    <RadixTabs.Trigger
      className={cn(
        "-mb-px shrink-0 border-b-2 border-transparent px-3 py-2 text-sm font-medium whitespace-nowrap text-slate-600 hover:text-slate-900 data-[state=active]:border-accent-600 data-[state=active]:text-accent-700",
        className,
      )}
      {...props}
    />
  );
}

export function TabPanel({ className, ...props }: ComponentProps<typeof RadixTabs.Content>) {
  return <RadixTabs.Content className={cn("pt-5 focus-visible:outline-offset-4", className)} {...props} />;
}
