"use client";

import { Fragment, useState } from "react";
import { ChevronRight, UserRound, X } from "lucide-react";
import { UserPicker } from "@/components/shared/user-picker";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { EmptyState, ErrorNotice, SkeletonRows } from "@/components/ui/feedback";
import { Field, Input } from "@/components/ui/form";
import { Pagination } from "@/components/ui/pagination";
import type { AuditQuery, OrgMember } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { formatDateTime } from "@/lib/format";
import { useAuditEvents } from "@/lib/hooks/admin";
import { useDebouncedCallback } from "@/lib/hooks/url-state";

// Stable event families (backend/app/services/audit.py); the field also accepts any exact action.
const ACTION_SUGGESTIONS = ["auth.*", "organization.*", "workspace.*", "document.*", "conversation.*", "security.*"];
const RESOURCE_SUGGESTIONS = ["document", "workspace", "user", "conversation", "invitation", "endpoint"];

const toIso = (local: string) => (local ? new Date(local).toISOString() : undefined);

export function AuditLog() {
  const [action, setAction] = useState("");
  const [resourceType, setResourceType] = useState("");
  const [resourceId, setResourceId] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [actor, setActor] = useState<OrgMember | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [open, setOpen] = useState<string | null>(null);
  const [resetKey, setResetKey] = useState(0); // remounts the uncontrolled filter inputs

  const query: AuditQuery = {
    action: action || undefined,
    actor_user_id: actor?.user_id,
    resource_type: resourceType || undefined,
    resource_id: resourceId || undefined,
    since: toIso(since),
    until: toIso(until),
    page,
    page_size: pageSize,
  };
  const events = useAuditEvents(query);
  const filtered = Boolean(action || actor || resourceType || resourceId || since || until);

  const debounced = (set: (value: string) => void) => (value: string) => {
    set(value);
    setPage(1);
  };
  const onAction = useDebouncedCallback(debounced(setAction), 300);
  const onResourceType = useDebouncedCallback(debounced(setResourceType), 300);
  const onResourceId = useDebouncedCallback(debounced(setResourceId), 300);

  return (
    <div>
      <form
        key={resetKey}
        className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4"
        onSubmit={(e) => e.preventDefault()}
        aria-label="Filter audit log"
      >
        <Field label="Action" hint="Exact action or a prefix ending in *">
          {(p) => (
            <Input
              {...p}
              list="audit-actions"
              placeholder="document.*"
              maxLength={100}
              onChange={(e) => onAction(e.target.value.trim())}
              data-testid="audit-action"
            />
          )}
        </Field>
        <datalist id="audit-actions">
          {ACTION_SUGGESTIONS.map((a) => (
            <option key={a} value={a} />
          ))}
        </datalist>
        <div>
          <span className="mb-1 block text-sm font-medium text-slate-700">Actor</span>
          <div className="flex items-center gap-1">
            <UserPicker
              onSelect={(member) => {
                setActor(member);
                setPage(1);
              }}
            >
              <Button className="min-w-0 flex-1 justify-start" data-testid="audit-actor">
                <UserRound aria-hidden className="size-4 shrink-0" />
                <span className="truncate">{actor ? actor.full_name : "Anyone"}</span>
              </Button>
            </UserPicker>
            {actor ? (
              <Button variant="ghost" size="sm" aria-label="Clear actor filter" onClick={() => setActor(null)}>
                <X aria-hidden className="size-4" />
              </Button>
            ) : null}
          </div>
        </div>
        <Field label="Resource type">
          {(p) => (
            <Input
              {...p}
              list="audit-resource-types"
              maxLength={50}
              onChange={(e) => onResourceType(e.target.value.trim())}
            />
          )}
        </Field>
        <datalist id="audit-resource-types">
          {RESOURCE_SUGGESTIONS.map((r) => (
            <option key={r} value={r} />
          ))}
        </datalist>
        <Field label="Resource ID">
          {(p) => <Input {...p} maxLength={100} onChange={(e) => onResourceId(e.target.value.trim())} />}
        </Field>
        <Field label="From">
          {(p) => (
            <Input
              {...p}
              type="datetime-local"
              value={since}
              onChange={(e) => {
                setSince(e.target.value);
                setPage(1);
              }}
            />
          )}
        </Field>
        <Field label="Until">
          {(p) => (
            <Input
              {...p}
              type="datetime-local"
              value={until}
              onChange={(e) => {
                setUntil(e.target.value);
                setPage(1);
              }}
            />
          )}
        </Field>
        {filtered ? (
          <div className="flex items-end">
            <Button
              variant="ghost"
              onClick={() => {
                setAction("");
                setActor(null);
                setResourceType("");
                setResourceId("");
                setSince("");
                setUntil("");
                setPage(1);
                setResetKey((key) => key + 1);
              }}
            >
              Clear filters
            </Button>
          </div>
        ) : null}
      </form>

      {events.isError ? (
        <ErrorNotice error={events.error} title="Could not load the audit log" onRetry={() => events.refetch()} />
      ) : events.isPending ? (
        <SkeletonRows rows={6} />
      ) : events.data.items.length === 0 ? (
        <EmptyState
          title="No audit events found"
          description={filtered ? "Try widening the filters." : "Security-relevant actions will be recorded here."}
        />
      ) : (
        <div className="space-y-4">
          <div
            className={cn(
              "relative overflow-x-auto rounded-lg border border-slate-200 bg-white",
              events.isPlaceholderData && "opacity-60",
            )}
          >
            <table className="w-full min-w-[44rem] text-left text-sm" data-testid="audit-table">
              <caption className="sr-only">Audit events, newest first</caption>
              <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-600">
                <tr>
                  <th scope="col" className="w-8 px-2 py-2">
                    <span className="sr-only">Details</span>
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Time
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Actor
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Action
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Resource
                  </th>
                  <th scope="col" className="hidden px-3 py-2 font-medium lg:table-cell">
                    IP address
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {events.data.items.map((event) => {
                  const expanded = open === event.id;
                  return (
                    <Fragment key={event.id}>
                      <tr data-testid="audit-row">
                        <td className="px-2 py-2">
                          <button
                            type="button"
                            aria-expanded={expanded}
                            aria-label={`${expanded ? "Hide" : "Show"} details for ${event.action}`}
                            onClick={() => setOpen(expanded ? null : event.id)}
                            className="rounded-sm p-1 text-slate-500 hover:bg-slate-100"
                          >
                            <ChevronRight
                              aria-hidden
                              className={cn("size-4 transition-transform", expanded && "rotate-90")}
                            />
                          </button>
                        </td>
                        <td className="px-3 py-2 whitespace-nowrap text-slate-700">
                          {formatDateTime(event.created_at)}
                        </td>
                        <td className="px-3 py-2 text-slate-700">{event.actor_email ?? "System"}</td>
                        <td className="px-3 py-2">
                          <code className="rounded-sm bg-slate-100 px-1.5 py-0.5 font-mono text-xs">
                            {event.action}
                          </code>
                        </td>
                        <td className="max-w-56 truncate px-3 py-2 text-slate-600">
                          {event.resource_type ?? "-"}
                          {event.resource_id ? (
                            <span className="text-slate-500"> · {event.resource_id.slice(0, 8)}</span>
                          ) : null}
                        </td>
                        <td className="hidden px-3 py-2 text-slate-600 lg:table-cell">{event.ip_address ?? "-"}</td>
                      </tr>
                      {expanded ? (
                        <tr className="bg-slate-50/70">
                          <td />
                          <td colSpan={5} className="px-3 py-3">
                            <dl className="grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
                              <div>
                                <dt className="text-slate-500">Resource ID</dt>
                                <dd className="font-mono break-all">{event.resource_id ?? "-"}</dd>
                              </div>
                              <div>
                                <dt className="text-slate-500">Request ID</dt>
                                <dd className="flex items-center gap-1 font-mono break-all">
                                  {event.request_id ?? "-"}
                                  {event.request_id ? (
                                    <CopyButton text={event.request_id} label="Copy request ID" iconOnly />
                                  ) : null}
                                </dd>
                              </div>
                            </dl>
                            <p className="mt-2 text-xs text-slate-500">Metadata</p>
                            <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-white p-2 font-mono text-xs ring-1 ring-slate-200">
                              {JSON.stringify(event.metadata, null, 2)}
                            </pre>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pagination
            page={events.data.page}
            pageSize={events.data.page_size}
            total={events.data.total}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setPage(1);
            }}
          />
        </div>
      )}
    </div>
  );
}
