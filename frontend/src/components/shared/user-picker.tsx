"use client";

import { useState, type ReactNode } from "react";
import { Input } from "@/components/ui/form";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { SkeletonRows } from "@/components/ui/feedback";
import { useDebouncedCallback } from "@/lib/hooks/url-state";
import { useOrgMembers } from "@/lib/hooks/workspaces";
import type { OrgMember } from "@/lib/api/types";
import { ErrorNotice } from "@/components/ui/feedback";

/** Search the organization directory and pick one person. The trigger is the child. */
export function UserPicker({
  children,
  onSelect,
  exclude,
}: {
  children: ReactNode;
  onSelect: (member: OrgMember) => void;
  /** User ids that cannot be picked (already added). */
  exclude?: ReadonlySet<string>;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const setDebounced = useDebouncedCallback((value: string) => setQuery(value), 250);
  const members = useOrgMembers({ q: query || undefined, page: 1, page_size: 20 });
  const candidates = (members.data?.items ?? []).filter((m) => !exclude?.has(m.user_id));

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setQuery("");
      }}
    >
      <PopoverTrigger asChild>{children}</PopoverTrigger>
      <PopoverContent>
        <Input
          placeholder="Search by name or email"
          aria-label="Search people"
          autoFocus
          onChange={(event) => setDebounced(event.target.value.trim())}
        />
        <div className="mt-2 max-h-64 overflow-y-auto" data-testid="user-picker-results">
          {members.isError ? (
            <ErrorNotice error={members.error} onRetry={() => members.refetch()} />
          ) : members.isPending ? (
            <SkeletonRows rows={3} />
          ) : candidates.length === 0 ? (
            <p className="px-2 py-4 text-center text-sm text-slate-500">No matching people.</p>
          ) : (
            <ul>
              {candidates.map((member) => (
                <li key={member.user_id}>
                  <button
                    type="button"
                    className="flex w-full flex-col rounded-md px-2 py-1.5 text-left hover:bg-slate-100"
                    onClick={() => {
                      onSelect(member);
                      setOpen(false);
                    }}
                  >
                    <span className="text-sm font-medium text-slate-900">{member.full_name}</span>
                    <span className="text-xs text-slate-500">{member.email}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
