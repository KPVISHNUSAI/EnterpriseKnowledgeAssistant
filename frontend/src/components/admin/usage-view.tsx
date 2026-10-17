"use client";

import { useState, type ReactNode } from "react";
import { Badge, Card, CardHeader, ErrorNotice, SkeletonRows } from "@/components/ui/feedback";
import { Select } from "@/components/ui/form";
import type { Usage } from "@/lib/api/types";
import { formatBytes, formatCompact, formatMs, formatNumber, formatUsd } from "@/lib/format";
import { useUsage } from "@/lib/hooks/admin";
import { DOCUMENT_STATUSES } from "@/lib/labels";
import { cn } from "@/lib/cn";
import { StatusBadge } from "@/components/documents/status-badge";
import { BarChart } from "./bar-chart";

const PERIODS = [7, 14, 30, 90, 365];

function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <dt className="text-xs font-medium text-slate-500">{label}</dt>
      <dd className="mt-1">
        <span className="block text-xl font-semibold text-slate-900 tabular-nums">{value}</span>
        {hint ? <span className="mt-0.5 block text-xs text-slate-500">{hint}</span> : null}
      </dd>
    </div>
  );
}

type Metric = "requests" | "tokens" | "cost";
const METRICS: Record<
  Metric,
  { label: string; pick: (day: Usage["daily"][number]) => number; format: (n: number) => string }
> = {
  requests: { label: "Chat requests", pick: (d) => d.chat_requests, format: formatNumber },
  tokens: { label: "Tokens", pick: (d) => d.input_tokens + d.output_tokens, format: formatCompact },
  cost: { label: "Estimated cost", pick: (d) => d.estimated_cost_usd, format: formatUsd },
};

function UsageContent({ usage }: { usage: Usage }) {
  const [metric, setMetric] = useState<Metric>("requests");
  const { totals, latency, ingestion } = usage;
  const config = METRICS[metric];
  const bars = usage.daily.map((day) => ({ label: day.date, value: config.pick(day) }));

  return (
    <div className="space-y-6">
      <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" data-testid="usage-totals">
        <Stat label="Documents" value={formatNumber(totals.documents)} hint={`${formatNumber(totals.chunks)} chunks`} />
        <Stat label="Storage" value={formatBytes(totals.storage_bytes)} />
        <Stat
          label="Conversations"
          value={formatNumber(totals.conversations)}
          hint={`${formatNumber(totals.messages)} messages`}
        />
        <Stat
          label="Chat requests"
          value={formatNumber(totals.chat_requests)}
          hint={`last ${usage.period_days} days`}
        />
        <Stat label="Input tokens" value={formatCompact(totals.input_tokens)} />
        <Stat label="Output tokens" value={formatCompact(totals.output_tokens)} />
        <Stat
          label="Estimated cost"
          value={formatUsd(totals.estimated_cost_usd)}
          hint="Based on configured token prices"
        />
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <dt className="text-xs font-medium text-slate-500">Documents by status</dt>
          <dd className="mt-2 flex flex-wrap gap-1.5">
            {DOCUMENT_STATUSES.map((status) => (
              <span key={status} className="inline-flex items-center gap-1">
                <StatusBadge status={status} />
                <span className="text-xs text-slate-600 tabular-nums">
                  {formatNumber(totals.documents_by_status[status] ?? 0)}
                </span>
              </span>
            ))}
          </dd>
        </div>
      </dl>

      <Card>
        <CardHeader
          title="Daily activity"
          actions={
            <div className="flex gap-1" role="group" aria-label="Chart metric">
              {(Object.keys(METRICS) as Metric[]).map((key) => (
                <button
                  key={key}
                  type="button"
                  aria-pressed={metric === key}
                  onClick={() => setMetric(key)}
                  className={cn(
                    "rounded-md px-2.5 py-1 text-xs font-medium",
                    metric === key ? "bg-accent-600 text-white" : "bg-slate-100 text-slate-700 hover:bg-slate-200",
                  )}
                >
                  {METRICS[key].label}
                </button>
              ))}
            </div>
          }
        />
        <div className="p-4">
          {bars.length === 0 ? (
            <p className="py-8 text-center text-sm text-slate-500">No activity in this period.</p>
          ) : (
            <BarChart
              bars={bars}
              title={`${config.label} per day, last ${usage.period_days} days`}
              format={config.format}
            />
          )}
        </div>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader title="Latency" description={`${formatNumber(latency.samples)} answered questions sampled`} />
          <table className="w-full text-left text-sm" data-testid="latency-table">
            <caption className="sr-only">Latency percentiles</caption>
            <thead className="border-b border-slate-200 text-xs text-slate-500">
              <tr>
                <th scope="col" className="px-4 py-2 font-medium">
                  Stage
                </th>
                <th scope="col" className="px-4 py-2 text-right font-medium">
                  p50
                </th>
                <th scope="col" className="px-4 py-2 text-right font-medium">
                  p95
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 tabular-nums">
              <tr>
                <th scope="row" className="px-4 py-2 font-normal">
                  Retrieval
                </th>
                <td className="px-4 py-2 text-right">{formatMs(latency.retrieval_p50_ms)}</td>
                <td className="px-4 py-2 text-right">{formatMs(latency.retrieval_p95_ms)}</td>
              </tr>
              <tr>
                <th scope="row" className="px-4 py-2 font-normal">
                  Generation
                </th>
                <td className="px-4 py-2 text-right">{formatMs(latency.generation_p50_ms)}</td>
                <td className="px-4 py-2 text-right">{formatMs(latency.generation_p95_ms)}</td>
              </tr>
            </tbody>
          </table>
        </Card>

        <Card>
          <CardHeader title="Ingestion health" description="Document processing jobs" />
          <dl className="grid grid-cols-2 gap-3 p-4" data-testid="ingestion-health">
            <div>
              <dt className="text-xs text-slate-500">Queued</dt>
              <dd className="text-lg font-semibold tabular-nums">{formatNumber(ingestion.queued)}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Running</dt>
              <dd className="text-lg font-semibold tabular-nums">{formatNumber(ingestion.running)}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Succeeded (24 h)</dt>
              <dd className="text-lg font-semibold text-emerald-700 tabular-nums">
                {formatNumber(ingestion.succeeded_last_24h)}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Failed (24 h)</dt>
              <dd className="flex items-center gap-2 text-lg font-semibold tabular-nums">
                <span className={ingestion.failed_last_24h > 0 ? "text-red-700" : undefined}>
                  {formatNumber(ingestion.failed_last_24h)}
                </span>
                {ingestion.failed_last_24h > 0 ? <Badge tone="danger">Needs attention</Badge> : null}
              </dd>
            </div>
          </dl>
        </Card>
      </div>
    </div>
  );
}

export function UsageView() {
  const [days, setDays] = useState(30);
  const usage = useUsage(days);
  return (
    <div>
      <div className="mb-4 flex items-center justify-end gap-2">
        <label htmlFor="usage-period" className="text-sm text-slate-600">
          Period
        </label>
        <Select id="usage-period" className="w-40" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {PERIODS.map((n) => (
            <option key={n} value={n}>
              Last {n} days
            </option>
          ))}
        </Select>
      </div>
      {usage.isError ? (
        <ErrorNotice error={usage.error} title="Could not load usage" onRetry={() => usage.refetch()} />
      ) : usage.isPending ? (
        <SkeletonRows rows={6} />
      ) : (
        <div
          className={usage.isPlaceholderData ? "opacity-60 transition-opacity" : undefined}
          aria-busy={usage.isFetching}
        >
          <UsageContent usage={usage.data} />
        </div>
      )}
    </div>
  );
}
