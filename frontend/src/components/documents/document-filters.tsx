"use client";

import { useRef } from "react";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field, Input, Select } from "@/components/ui/form";
import { FILE_TYPE_LABELS } from "@/lib/format";
import { useDebouncedCallback } from "@/lib/hooks/url-state";
import { useOrgMembers, useWorkspaces } from "@/lib/hooks/workspaces";
import { DOCUMENT_STATUSES, DOCUMENT_STATUS_LABELS, FILE_TYPES } from "@/lib/labels";

export interface DocumentFilterValues {
  q: string;
  workspace: string;
  status: string;
  type: string;
  uploader: string;
}

export function hasFilters(values: DocumentFilterValues) {
  return Object.values(values).some(Boolean);
}

export function DocumentFilters({
  values,
  onChange,
}: {
  values: DocumentFilterValues;
  onChange: (patch: Partial<DocumentFilterValues>) => void;
}) {
  const workspaces = useWorkspaces();
  // The directory is capped at 100 people per page; larger organizations narrow by workspace first.
  const people = useOrgMembers({ page: 1, page_size: 100 });
  const onSearch = useDebouncedCallback((q: string) => onChange({ q }), 300);
  const searchBox = useRef<HTMLInputElement>(null);

  return (
    <form
      role="search"
      aria-label="Filter documents"
      className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-6"
      onSubmit={(e) => e.preventDefault()}
    >
      <Field label="Search" hideLabel className="sm:col-span-2 lg:col-span-4 2xl:col-span-2">
        {(p) => (
          <Input
            {...p}
            ref={searchBox}
            type="search"
            placeholder="Search title or filename"
            defaultValue={values.q}
            onChange={(event) => onSearch(event.target.value.trim())}
            data-testid="document-search"
          />
        )}
      </Field>
      <Field label="Workspace" hideLabel>
        {(p) => (
          <Select {...p} value={values.workspace} onChange={(e) => onChange({ workspace: e.target.value })}>
            <option value="">All workspaces</option>
            {workspaces.data?.map((ws) => (
              <option key={ws.id} value={ws.id}>
                {ws.name}
              </option>
            ))}
          </Select>
        )}
      </Field>
      <Field label="Status" hideLabel>
        {(p) => (
          <Select {...p} value={values.status} onChange={(e) => onChange({ status: e.target.value })}>
            <option value="">Any status</option>
            {DOCUMENT_STATUSES.map((status) => (
              <option key={status} value={status}>
                {DOCUMENT_STATUS_LABELS[status]}
              </option>
            ))}
          </Select>
        )}
      </Field>
      <Field label="File type" hideLabel>
        {(p) => (
          <Select {...p} value={values.type} onChange={(e) => onChange({ type: e.target.value })}>
            <option value="">Any type</option>
            {FILE_TYPES.map((type) => (
              <option key={type} value={type}>
                {FILE_TYPE_LABELS[type]}
              </option>
            ))}
          </Select>
        )}
      </Field>
      <Field label="Uploaded by" hideLabel>
        {(p) => (
          <Select {...p} value={values.uploader} onChange={(e) => onChange({ uploader: e.target.value })}>
            <option value="">Anyone</option>
            {people.data?.items.map((person) => (
              <option key={person.user_id} value={person.user_id}>
                {person.full_name}
              </option>
            ))}
          </Select>
        )}
      </Field>
      {hasFilters(values) ? (
        <div className="sm:col-span-2 lg:col-span-4 2xl:col-span-6">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              if (searchBox.current) searchBox.current.value = "";
              onChange({ q: "", workspace: "", status: "", type: "", uploader: "" });
            }}
          >
            <X aria-hidden className="size-3.5" /> Clear filters
          </Button>
        </div>
      ) : null}
    </form>
  );
}
