"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  FileText,
  Layers,
  Loader2,
  MessageSquare,
  Search,
  Upload,
} from "lucide-react";
import { useSession } from "@/components/auth/session";
import { StatusBadge } from "@/components/documents/status-badge";
import { LinkButton } from "@/components/ui/button";
import { Card, CardHeader, EmptyState, ErrorNotice, PageHeader, Skeleton } from "@/components/ui/feedback";
import { Time } from "@/components/ui/time";
import { formatNumber, pluralize } from "@/lib/format";
import { useDashboard } from "@/lib/hooks/dashboard";
import { cn } from "@/lib/cn";

function Stat({
  label,
  value,
  icon,
  href,
  tone,
}: {
  label: string;
  value: number;
  icon: ReactNode;
  href: string;
  tone?: "danger";
}) {
  return (
    <Link
      href={href}
      className="rounded-lg border border-slate-200 bg-white p-4 shadow-xs transition-colors hover:border-slate-300 hover:bg-slate-50/60"
    >
      <div className="flex items-center gap-2 text-sm text-slate-600">
        <span className={cn("[&>svg]:size-4", tone === "danger" && value > 0 ? "text-red-600" : "text-slate-400")}>
          {icon}
        </span>
        {label}
      </div>
      <p
        className={cn(
          "mt-2 text-2xl font-semibold tabular-nums",
          tone === "danger" && value > 0 ? "text-red-700" : "text-slate-900",
        )}
      >
        {formatNumber(value)}
      </p>
    </Link>
  );
}

export default function DashboardPage() {
  const { organization, capabilities, user } = useSession();
  const dashboard = useDashboard();

  return (
    <>
      <PageHeader
        title={`Welcome back, ${user.full_name.split(" ")[0]}`}
        description={`Overview of ${organization.name}`}
        actions={
          <>
            <LinkButton href="/chat" variant="primary">
              <MessageSquare aria-hidden className="size-4" /> Ask a question
            </LinkButton>
            <LinkButton href="/search">
              <Search aria-hidden className="size-4" /> Search
            </LinkButton>
            {capabilities.can_upload ? (
              <LinkButton href="/documents">
                <Upload aria-hidden className="size-4" /> Upload
              </LinkButton>
            ) : null}
          </>
        }
      />

      {dashboard.isError ? (
        <ErrorNotice error={dashboard.error} title="Could not load the dashboard" onRetry={() => dashboard.refetch()} />
      ) : dashboard.isPending ? (
        <div className="space-y-6" role="status" aria-label="Loading dashboard">
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
            {Array.from({ length: 5 }, (_, i) => (
              <Skeleton key={i} className="h-24" />
            ))}
          </div>
          <div className="grid gap-6 lg:grid-cols-2">
            <Skeleton className="h-64" />
            <Skeleton className="h-64" />
          </div>
        </div>
      ) : (
        <DashboardContent data={dashboard.data} canUpload={capabilities.can_upload} />
      )}
    </>
  );
}

function DashboardContent({
  data,
  canUpload,
}: {
  data: NonNullable<ReturnType<typeof useDashboard>["data"]>;
  canUpload: boolean;
}) {
  const by = data.documents_by_status;
  return (
    <div className="space-y-6">
      <section
        aria-label="Document counts"
        className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5"
        data-testid="dashboard-stats"
      >
        <Stat label="Documents" value={data.documents_total} icon={<FileText />} href="/documents" />
        <Stat label="Indexed" value={by.indexed ?? 0} icon={<CheckCircle2 />} href="/documents?status=indexed" />
        <Stat
          label="In progress"
          value={(by.queued ?? 0) + (by.processing ?? 0)}
          icon={<Loader2 />}
          href="/documents?status=processing"
        />
        <Stat
          label="Failed"
          value={by.failed ?? 0}
          icon={<AlertCircle />}
          href="/documents?status=failed"
          tone="danger"
        />
        <Stat label="Workspaces" value={data.workspaces_count} icon={<Layers />} href="/workspaces" />
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Recent documents"
            actions={
              <Link href="/documents" className="text-sm text-accent-700 hover:underline">
                View all
              </Link>
            }
          />
          {data.recent_documents.length === 0 ? (
            <EmptyState
              className="m-4 border-0"
              icon={<FileText />}
              title="No documents yet"
              description={
                canUpload
                  ? "Upload documents to start asking questions about them."
                  : "Documents shared with you will appear here."
              }
            />
          ) : (
            <ul className="divide-y divide-slate-100">
              {data.recent_documents.map((doc) => (
                <li key={doc.id} className="flex items-center justify-between gap-3 px-4 py-3">
                  <div className="min-w-0">
                    <Link
                      href={`/documents/${doc.id}`}
                      className="block truncate text-sm font-medium text-slate-900 hover:text-accent-700 hover:underline"
                    >
                      {doc.title}
                    </Link>
                    <p className="truncate text-xs text-slate-500">
                      {doc.workspace_name} · <Time iso={doc.created_at} />
                    </p>
                  </div>
                  <StatusBadge status={doc.processing_status} error={doc.processing_error} />
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card>
          <CardHeader
            title="Recent conversations"
            actions={
              <Link href="/chat" className="text-sm text-accent-700 hover:underline">
                New chat
              </Link>
            }
          />
          {data.recent_conversations.length === 0 ? (
            <EmptyState
              className="m-4 border-0"
              icon={<MessageSquare />}
              title="No conversations yet"
              description="Ask a question and answers will cite the documents they came from."
              action={
                <LinkButton href="/chat" variant="primary">
                  Start a chat
                </LinkButton>
              }
            />
          ) : (
            <ul className="divide-y divide-slate-100">
              {data.recent_conversations.map((conversation) => (
                <li key={conversation.id} className="flex items-center justify-between gap-3 px-4 py-3">
                  <Link
                    href={`/chat/${conversation.id}`}
                    className="min-w-0 truncate text-sm font-medium text-slate-900 hover:text-accent-700 hover:underline"
                  >
                    {conversation.title}
                  </Link>
                  <span className="shrink-0 text-xs text-slate-500">
                    {pluralize(conversation.message_count, "message")} · <Time iso={conversation.updated_at} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card>
        <CardHeader title="Recent activity" description="Changes you are allowed to see" />
        {data.recent_activity.length === 0 ? (
          <p className="px-4 py-8 text-center text-sm text-slate-500">No recent activity.</p>
        ) : (
          <ul className="divide-y divide-slate-100" data-testid="recent-activity">
            {data.recent_activity.map((item) => (
              <li key={item.id} className="flex items-start gap-3 px-4 py-3">
                <Activity aria-hidden className="mt-0.5 size-4 shrink-0 text-slate-400" />
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-slate-800">{item.summary}</p>
                  <p className="text-xs text-slate-500">
                    {item.actor_email ?? "System"} · <Time iso={item.created_at} />
                  </p>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
