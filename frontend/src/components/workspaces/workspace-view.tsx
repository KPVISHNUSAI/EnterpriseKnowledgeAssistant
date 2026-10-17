"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ChevronRight, FileText, Layers, Pencil, Trash2, Users } from "lucide-react";
import { Button, LinkButton } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { Badge, Card, EmptyState, ErrorNotice, Skeleton } from "@/components/ui/feedback";
import { ApiError } from "@/lib/api/errors";
import { pluralize } from "@/lib/format";
import { useDeleteWorkspace, useWorkspace } from "@/lib/hooks/workspaces";
import { WORKSPACE_ROLE_LABELS } from "@/lib/labels";
import { EditWorkspaceDialog } from "./workspace-dialog";
import { WorkspaceMembers } from "./workspace-members";

export function WorkspaceView({ id }: { id: string }) {
  const router = useRouter();
  const confirm = useConfirm();
  const query = useWorkspace(id);
  const remove = useDeleteWorkspace();
  const [editing, setEditing] = useState(false);

  if (query.isPending) {
    return (
      <div className="space-y-4" role="status" aria-label="Loading workspace">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-24" />
        <Skeleton className="h-64" />
      </div>
    );
  }
  if (query.isError) {
    if (query.error instanceof ApiError && query.error.status === 404) {
      return (
        <EmptyState
          icon={<Layers />}
          title="Workspace not found"
          description="It may have been deleted, or you may not be a member."
          action={<LinkButton href="/workspaces">Back to workspaces</LinkButton>}
        />
      );
    }
    return <ErrorNotice error={query.error} title="Could not load the workspace" onRetry={() => query.refetch()} />;
  }

  const workspace = query.data;

  async function onDelete() {
    const ok = await confirm({
      title: `Delete “${workspace.name}”?`,
      description:
        workspace.document_count > 0
          ? "This workspace still contains documents. Delete or move them first; the server will refuse otherwise."
          : "The workspace and its member list will be permanently deleted.",
      confirmLabel: "Delete workspace",
      destructive: true,
    });
    if (ok) remove.mutate(workspace.id, { onSuccess: () => router.replace("/workspaces") });
  }

  return (
    <div className="space-y-6">
      <title>{`${workspace.name} | Knowledge Assistant`}</title>
      <nav aria-label="Breadcrumb" className="flex items-center gap-1 text-sm text-slate-500">
        <Link href="/workspaces" className="hover:text-slate-900 hover:underline">
          Workspaces
        </Link>
        <ChevronRight aria-hidden className="size-3.5" />
        <span className="truncate text-slate-700" aria-current="page">
          {workspace.name}
        </span>
      </nav>

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight break-words text-slate-900" data-testid="workspace-title">
            {workspace.name}
          </h1>
          <p className="mt-1 max-w-2xl text-sm text-slate-600">{workspace.description || "No description"}</p>
          <p className="mt-2 flex flex-wrap items-center gap-3 text-sm text-slate-600">
            <Badge tone={workspace.my_role ? "accent" : "neutral"}>
              {workspace.my_role ? WORKSPACE_ROLE_LABELS[workspace.my_role] : "Admin access"}
            </Badge>
            <span className="flex items-center gap-1">
              <Users aria-hidden className="size-4 text-slate-400" /> {pluralize(workspace.member_count, "member")}
            </span>
            <Link
              href={`/documents?workspace=${workspace.id}`}
              className="flex items-center gap-1 text-accent-700 hover:underline"
            >
              <FileText aria-hidden className="size-4" /> {pluralize(workspace.document_count, "document")}
            </Link>
          </p>
        </div>
        {workspace.can_manage ? (
          <div className="flex gap-2">
            <Button onClick={() => setEditing(true)}>
              <Pencil aria-hidden className="size-4" /> Edit
            </Button>
            <Button
              variant="danger-outline"
              onClick={onDelete}
              loading={remove.isPending}
              data-testid="delete-workspace"
            >
              <Trash2 aria-hidden className="size-4" /> Delete
            </Button>
          </div>
        ) : null}
      </div>

      {!workspace.can_manage ? (
        <Card className="p-4 text-sm text-slate-600">
          You are {workspace.my_role ? `a ${workspace.my_role}` : "a viewer"} of this workspace. Only owners and
          organization administrators can change its settings and members.
        </Card>
      ) : null}

      <WorkspaceMembers workspaceId={workspace.id} canManage={workspace.can_manage} />
      <EditWorkspaceDialog open={editing} onOpenChange={setEditing} workspace={workspace} />
    </div>
  );
}
