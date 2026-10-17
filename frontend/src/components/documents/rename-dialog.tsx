"use client";

import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/form";
import { useRenameDocument } from "@/lib/hooks/documents";

export function RenameDialog({
  documentId,
  title,
  open,
  onOpenChange,
}: {
  documentId: string;
  title: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const rename = useRenameDocument(documentId);
  const [error, setError] = useState<string | null>(null);

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = String(new FormData(event.currentTarget).get("title") ?? "").trim();
    if (!next) return setError("Enter a title.");
    setError(null);
    if (next === title) return onOpenChange(false);
    rename.mutate(next, { onSuccess: () => onOpenChange(false) });
  }

  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Rename document" size="sm">
      <form onSubmit={onSubmit} noValidate className="space-y-4">
        <Field label="Title" error={error}>
          {(p) => (
            <Input
              {...p}
              name="title"
              defaultValue={title}
              maxLength={500}
              autoFocus
              required
              data-testid="rename-input"
            />
          )}
        </Field>
        <div className="flex justify-end gap-2">
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={rename.isPending}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}
