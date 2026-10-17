"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { FileText, Layers, Plus, Users } from "lucide-react";
import { useSession } from "@/components/auth/session";
import { CreateWorkspaceDialog } from "@/components/workspaces/workspace-dialog";
import { Button } from "@/components/ui/button";
import { Badge, Card, EmptyState, ErrorNotice, PageHeader, Skeleton } from "@/components/ui/feedback";
import { pluralize } from "@/lib/format";
import { useWorkspaces } from "@/lib/hooks/workspaces";
import { WORKSPACE_ROLE_LABELS } from "@/lib/labels";

export default function WorkspacesPage() {
  const router = useRouter();
  const { capabilities } = useSession();
  const workspaces = useWorkspaces();
  const [creating, setCreating] = useState(false);

  const createButton = capabilities.can_create_workspace ? (
    <Button variant="primary" onClick={() => setCreating(true)} data-testid="new-workspace">
      <Plus aria-hidden className="size-4" /> New workspace
    </Button>
  ) : null;

  return (
    <>
      <PageHeader
        title="Workspaces"
        description="Groups of documents, with their own members and permissions."
        actions={createButton}
      />
      {workspaces.isError ? (
        <ErrorNotice error={workspaces.error} title="Could not load workspaces" onRetry={() => workspaces.refetch()} />
      ) : workspaces.isPending ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" role="status" aria-label="Loading workspaces">
          {Array.from({ length: 3 }, (_, i) => (
            <Skeleton key={i} className="h-32" />
          ))}
        </div>
      ) : workspaces.data.length === 0 ? (
        <EmptyState
          icon={<Layers />}
          title="No workspaces yet"
          description={
            capabilities.can_create_workspace
              ? "Create a workspace to start organizing documents."
              : "You are not a member of any workspace. Ask an administrator to add you."
          }
          action={createButton}
        />
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" data-testid="workspace-list">
          {workspaces.data.map((workspace) => (
            <li key={workspace.id}>
              <Card className="h-full transition-colors hover:border-slate-300">
                <Link href={`/workspaces/${workspace.id}`} className="block h-full rounded-lg p-4">
                  <div className="flex items-start justify-between gap-2">
                    <h2 className="min-w-0 truncate text-base font-semibold text-slate-900">{workspace.name}</h2>
                    <Badge tone={workspace.my_role ? "accent" : "neutral"}>
                      {workspace.my_role ? WORKSPACE_ROLE_LABELS[workspace.my_role] : "Admin access"}
                    </Badge>
                  </div>
                  <p className="mt-1 line-clamp-2 min-h-10 text-sm text-slate-600">
                    {workspace.description || "No description"}
                  </p>
                  <p className="mt-3 flex items-center gap-4 text-xs text-slate-500">
                    <span className="flex items-center gap-1">
                      <Users aria-hidden className="size-3.5" /> {pluralize(workspace.member_count, "member")}
                    </span>
                    <span className="flex items-center gap-1">
                      <FileText aria-hidden className="size-3.5" /> {pluralize(workspace.document_count, "document")}
                    </span>
                  </p>
                </Link>
              </Card>
            </li>
          ))}
        </ul>
      )}
      <CreateWorkspaceDialog
        open={creating}
        onOpenChange={setCreating}
        onCreated={(workspace) => router.push(`/workspaces/${workspace.id}`)}
      />
    </>
  );
}
