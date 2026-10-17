"use client";

import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/dialog";
import { ErrorNotice } from "@/components/ui/feedback";
import { Field, Input, Textarea } from "@/components/ui/form";
import { fieldErrors } from "@/lib/api/errors";
import type { Workspace } from "@/lib/api/types";
import { useCreateWorkspace, useUpdateWorkspace } from "@/lib/hooks/workspaces";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function WorkspaceForm({
  initial,
  onSubmit,
  pending,
  error,
  submitLabel,
  onCancel,
}: {
  initial?: Pick<Workspace, "name" | "description">;
  onSubmit: (values: { name: string; description: string | null }) => void;
  pending: boolean;
  error: unknown;
  submitLabel: string;
  onCancel: () => void;
}) {
  const [nameError, setNameError] = useState<string | null>(null);
  const serverFields = fieldErrors(error);

  function handle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const name = String(form.get("name") ?? "").trim();
    const description = String(form.get("description") ?? "").trim();
    if (!name) return setNameError("Enter a name.");
    setNameError(null);
    onSubmit({ name, description: description || null });
  }

  return (
    <form onSubmit={handle} noValidate className="space-y-4">
      {error && Object.keys(serverFields).length === 0 ? (
        <ErrorNotice error={error} title="Could not save the workspace" />
      ) : null}
      <Field label="Name" error={nameError ?? serverFields.name}>
        {(p) => (
          <Input
            {...p}
            name="name"
            defaultValue={initial?.name}
            maxLength={200}
            autoFocus
            required
            data-testid="workspace-name"
          />
        )}
      </Field>
      <Field label="Description" hint="Optional. Shown to workspace members." error={serverFields.description}>
        {(p) => (
          <Textarea
            {...p}
            name="description"
            rows={3}
            defaultValue={initial?.description ?? ""}
            maxLength={2000}
            data-testid="workspace-description"
          />
        )}
      </Field>
      <div className="flex justify-end gap-2">
        <Button onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="primary" loading={pending} data-testid="workspace-save">
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}

export function CreateWorkspaceDialog({
  open,
  onOpenChange,
  onCreated,
}: Props & { onCreated: (workspace: Workspace) => void }) {
  const create = useCreateWorkspace();
  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="New workspace"
      description="Workspaces group documents and decide who can read them."
      size="md"
    >
      <WorkspaceForm
        pending={create.isPending}
        error={create.error}
        submitLabel="Create workspace"
        onCancel={() => onOpenChange(false)}
        onSubmit={(values) =>
          create.mutate(values, {
            onSuccess: (workspace) => {
              onOpenChange(false);
              onCreated(workspace);
            },
          })
        }
      />
    </Modal>
  );
}

export function EditWorkspaceDialog({ open, onOpenChange, workspace }: Props & { workspace: Workspace }) {
  const update = useUpdateWorkspace(workspace.id);
  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Edit workspace" size="md">
      <WorkspaceForm
        initial={workspace}
        pending={update.isPending}
        error={update.error}
        submitLabel="Save changes"
        onCancel={() => onOpenChange(false)}
        onSubmit={(values) => update.mutate(values, { onSuccess: () => onOpenChange(false) })}
      />
    </Modal>
  );
}
