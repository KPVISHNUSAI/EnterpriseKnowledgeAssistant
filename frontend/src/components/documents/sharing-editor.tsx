"use client";

import { useState } from "react";
import { Lock, Trash2, UserPlus, Users } from "lucide-react";
import { UserPicker } from "@/components/shared/user-picker";
import { Button, IconButton } from "@/components/ui/button";
import { Alert, Card, CardHeader } from "@/components/ui/feedback";
import { Select } from "@/components/ui/form";
import type { DocumentDetail, DocumentVisibility, PermissionLevel } from "@/lib/api/types";
import { useUpdatePermissions } from "@/lib/hooks/documents";

interface Grant {
  user_id: string;
  name: string;
  permission: PermissionLevel;
}

function initialGrants(doc: DocumentDetail): Grant[] {
  return (doc.permissions ?? [])
    .filter((p) => p.principal_type === "user")
    .map((p) => ({ user_id: p.principal_id, name: p.principal_name, permission: p.permission }));
}

/** Workspace-wide vs restricted visibility, plus per-user read/manage grants. */
export function SharingEditor({ doc }: { doc: DocumentDetail }) {
  const save = useUpdatePermissions(doc.id);
  const [visibility, setVisibility] = useState<DocumentVisibility>(doc.visibility);
  const [grants, setGrants] = useState<Grant[]>(() => initialGrants(doc));

  const original = initialGrants(doc);
  const fingerprint = (list: Grant[]) => JSON.stringify(list.map((g) => [g.user_id, g.permission]).sort());
  const dirty = visibility !== doc.visibility || fingerprint(grants) !== fingerprint(original);

  return (
    <Card>
      <CardHeader
        title="Who can access this document"
        description="Changes apply to search and chat answers immediately."
      />
      <div className="space-y-5 p-4">
        <fieldset className="space-y-2">
          <legend className="sr-only">Visibility</legend>
          <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-slate-200 p-3 has-checked:border-accent-500 has-checked:bg-accent-50/50">
            <input
              type="radio"
              name="visibility"
              value="workspace"
              checked={visibility === "workspace"}
              onChange={() => setVisibility("workspace")}
              className="mt-1 accent-accent-600"
            />
            <span>
              <span className="flex items-center gap-2 text-sm font-medium text-slate-900">
                <Users aria-hidden className="size-4 text-slate-500" /> Everyone in {doc.workspace_name}
              </span>
              <span className="mt-0.5 block text-sm text-slate-600">
                Every workspace member can read this document.
              </span>
            </span>
          </label>
          <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-slate-200 p-3 has-checked:border-accent-500 has-checked:bg-accent-50/50">
            <input
              type="radio"
              name="visibility"
              value="restricted"
              checked={visibility === "restricted"}
              onChange={() => setVisibility("restricted")}
              className="mt-1 accent-accent-600"
            />
            <span>
              <span className="flex items-center gap-2 text-sm font-medium text-slate-900">
                <Lock aria-hidden className="size-4 text-slate-500" /> Restricted
              </span>
              <span className="mt-0.5 block text-sm text-slate-600">
                Only the people listed below can read it. The uploader, workspace owners and organization admins always
                keep access.
              </span>
            </span>
          </label>
        </fieldset>

        <div>
          <div className="mb-2 flex items-center justify-between">
            <div>
              <h3 className="text-sm font-medium text-slate-800">
                {visibility === "restricted" ? "People with access" : "Individual access"}
              </h3>
              {visibility === "workspace" ? (
                <p className="text-xs text-slate-500">Optional: for example, let a colleague manage this document.</p>
              ) : null}
            </div>
            <UserPicker
              exclude={new Set(grants.map((g) => g.user_id))}
              onSelect={(member) =>
                setGrants((list) => [...list, { user_id: member.user_id, name: member.full_name, permission: "read" }])
              }
            >
              <Button size="sm" data-testid="add-grant">
                <UserPlus aria-hidden className="size-4" /> Add person
              </Button>
            </UserPicker>
          </div>
          {grants.length === 0 ? (
            <Alert tone="info">No one has been added yet.</Alert>
          ) : (
            <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200" data-testid="grants">
              {grants.map((grant) => (
                <li key={grant.user_id} className="flex items-center justify-between gap-3 px-3 py-2">
                  <span className="min-w-0 truncate text-sm text-slate-900">{grant.name}</span>
                  <div className="flex items-center gap-1">
                    <Select
                      aria-label={`Permission for ${grant.name}`}
                      className="h-8 w-36 text-[13px]"
                      value={grant.permission}
                      onChange={(event) =>
                        setGrants((list) =>
                          list.map((g) =>
                            g.user_id === grant.user_id
                              ? { ...g, permission: event.target.value as PermissionLevel }
                              : g,
                          ),
                        )
                      }
                    >
                      <option value="read">Can read</option>
                      <option value="manage">Can manage</option>
                    </Select>
                    <IconButton
                      label={`Remove ${grant.name}`}
                      onClick={() => setGrants((list) => list.filter((g) => g.user_id !== grant.user_id))}
                    >
                      <Trash2 aria-hidden className="size-4" />
                    </IconButton>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex justify-end gap-2">
          <Button
            disabled={!dirty}
            onClick={() => {
              setVisibility(doc.visibility);
              setGrants(original);
            }}
          >
            Reset
          </Button>
          <Button
            variant="primary"
            disabled={!dirty}
            loading={save.isPending}
            data-testid="save-sharing"
            onClick={() =>
              save.mutate({
                visibility,
                users: grants.map((g) => ({ user_id: g.user_id, permission: g.permission })),
              })
            }
          >
            Save changes
          </Button>
        </div>
      </div>
    </Card>
  );
}
