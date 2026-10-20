"use client";

import { useState, type FormEvent, type ReactNode } from "react";
import { ArrowLeft, FlaskConical, Printer, ShieldCheck, UserRound } from "lucide-react";
import { UserPicker } from "@/components/shared/user-picker";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, ErrorNotice, SkeletonRows, Spinner } from "@/components/ui/feedback";
import { Field, Select } from "@/components/ui/form";
import { Pagination } from "@/components/ui/pagination";
import { Time } from "@/components/ui/time";
import type { OrgMember, ProofFinding, ProofRun, ProofRunDetail } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { formatMs, formatNumber, pluralize } from "@/lib/format";
import { useProofRun, useProofRuns, useStartProof } from "@/lib/hooks/admin";
import { useUrlState } from "@/lib/hooks/url-state";
import { useWorkspaces } from "@/lib/hooks/workspaces";
import { WORKSPACE_ROLE_LABELS } from "@/lib/labels";

type Tone = "success" | "danger" | "warning" | "info";
type Summary = Partial<Record<string, number | string>>;

const KIND_LABELS = { manual: "Run by an admin", scheduled: "Weekly run", simulation: "Simulation" } as const;

const FINDING_LABELS: Record<ProofFinding["kind"], string> = {
  prompt_text: "Its text was in the model request",
  retrieved: "Retrieved as a source",
  access_extra: "The app lets the group read it; the rule does not",
  access_missing: "The rule lets the group read it; the app does not",
};

const num = (summary: Summary, key: string) => {
  const value = summary[key];
  return typeof value === "number" ? value : 0;
};

const personName = (run: ProofRun) => run.simulation?.person.full_name ?? "a former member";
const workspaceName = (run: ProofRun) => run.simulation?.workspace.name ?? "a deleted workspace";

/** The one-line result: what an auditor reads first. Simulations pass when they are caught. */
function outcome(run: ProofRun, documents?: number): { tone: Tone; label: string; text: string } {
  const summary = run.summary as Summary;
  if (run.status === "queued") return { tone: "info", label: "Queued", text: "Waiting for a worker to start." };
  if (run.status === "running") return { tone: "info", label: "Running", text: "Asking as every access group." };
  if (run.status === "interrupted")
    return {
      tone: "warning",
      label: "Interrupted",
      text: "The run stopped without finishing, for example because the worker restarted. Run it again.",
    };
  if (run.status === "error") return { tone: "danger", label: "Did not finish", text: run.error ?? "The run failed." };
  const docs = documents === undefined ? "" : ` about ${pluralize(documents, "document")}`;
  if (run.kind === "simulation") {
    if (run.verdict === "failed")
      return {
        tone: "success",
        label: "Caught",
        text: `The test reported ${pluralize(num(summary, "findings"), "finding")}${docs} for ${personName(run)}, who was treated as wrongly given ${workspaceName(run)}.`,
      };
    if (run.verdict === "passed")
      return {
        tone: "danger",
        label: "Not caught",
        text: `The simulated access to ${workspaceName(run)} for ${personName(run)} went unreported. Do not rely on the leak test until this is explained.`,
      };
    return {
      tone: "info",
      label: "Nothing to test",
      text: `By the time the run started, ${workspaceName(run)} held no indexed document that ${personName(run)} cannot read.`,
    };
  }
  if (run.verdict === "passed") {
    const sampled = num(summary, "probes_run") < num(summary, "probes_planned");
    return {
      tone: "success",
      label: sampled ? "Passed (sample)" : "Passed",
      text: `No leak found in ${pluralize(num(summary, "probes_run"), "question")} asked as ${pluralize(num(summary, "groups"), "access group")}: nothing found or sent to the model came from a document the group cannot read.${sampled ? ` The questions were a sample of the ${formatNumber(num(summary, "probes_planned"))} planned.` : ""}`,
    };
  }
  if (run.verdict === "failed")
    return {
      tone: "danger",
      label: "Failed",
      text: `${pluralize(num(summary, "findings"), "finding")}${docs}: the assistant would have used documents some people cannot read, or permissions disagree with the rule.`,
    };
  return {
    tone: "info",
    label: "Nothing to test",
    text: "Every access group can read every indexed document, so there was nothing to ask about.",
  };
}

function OutcomeBadge({ run }: { run: ProofRun }) {
  const { tone, label } = outcome(run);
  return (
    <Badge tone={tone} data-testid="proof-outcome">
      {label}
    </Badge>
  );
}

function RunKind({ run }: { run: ProofRun }) {
  if (run.kind !== "simulation") return <>{KIND_LABELS[run.kind]}</>;
  return (
    <>
      Simulation: {personName(run)} in {workspaceName(run)}
    </>
  );
}

function SimulateDialog({
  open,
  onOpenChange,
  onStarted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onStarted: (run: ProofRun) => void;
}) {
  const start = useStartProof();
  const workspaces = useWorkspaces();
  const [person, setPerson] = useState<OrgMember | null>(null);
  const [workspaceId, setWorkspaceId] = useState("");

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      setPerson(null);
      setWorkspaceId("");
      start.reset();
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!person || !workspaceId) return;
    start.mutate(
      { simulate: { user_id: person.user_id, workspace_id: workspaceId } },
      {
        onSuccess: (run) => {
          close(false);
          onStarted(run);
        },
      },
    );
  }

  return (
    <Modal
      open={open}
      onOpenChange={(next) => (start.isPending ? undefined : close(next))}
      title="Simulate a mis-share"
      description="Test one person as if the application had wrongly let them read a workspace. Nothing is changed: their real access stays as it is. The test must report it."
    >
      <form onSubmit={onSubmit} noValidate className="space-y-4">
        {start.error ? <ErrorNotice error={start.error} title="Could not start the simulation" /> : null}
        <div>
          <span id="proof-person-label" className="mb-1 block text-sm font-medium text-slate-700">
            Person
          </span>
          <UserPicker onSelect={setPerson}>
            <Button
              className="w-full justify-start"
              aria-labelledby="proof-person-label proof-person-value"
              data-testid="proof-person"
            >
              <UserRound aria-hidden className="size-4 shrink-0" />
              <span id="proof-person-value" className="truncate">
                {person ? `${person.full_name} (${person.email})` : "Choose a person"}
              </span>
            </Button>
          </UserPicker>
        </div>
        <Field label="Workspace they are not in" hint="It needs indexed documents the person cannot read.">
          {(p) => (
            <Select
              {...p}
              value={workspaceId}
              onChange={(e) => setWorkspaceId(e.target.value)}
              disabled={!workspaces.data}
              data-testid="proof-workspace"
            >
              <option value="">Choose a workspace</option>
              {workspaces.data?.map((ws) => (
                <option key={ws.id} value={ws.id}>
                  {ws.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <div className="flex justify-end gap-2">
          <Button onClick={() => close(false)} disabled={start.isPending}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={start.isPending}
            disabled={!person || !workspaceId}
            data-testid="proof-simulate-submit"
          >
            Run simulation
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function Overview({ onOpen }: { onOpen: (id: string) => void }) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [simulating, setSimulating] = useState(false);
  const runs = useProofRuns({ page, page_size: pageSize });
  const start = useStartProof();
  // ponytail: the latest real result is looked for on the first history page only (no
  // banner after 20+ simulations in a row); add a kind filter to the list endpoint if needed.
  const firstPage = useProofRuns({ page: 1, page_size: 20 });
  const latest = firstPage.data?.items.find((run) => run.kind !== "simulation");

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <p className="max-w-2xl text-sm text-slate-600">
          The leak test asks the assistant, as every access group in the organization, about every document that group
          cannot read, and checks what would have been sent to the model. Scheduled runs (weekly by default) appear here
          too. Nothing is changed and no document text is stored.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => setSimulating(true)} data-testid="proof-simulate-open">
            <FlaskConical aria-hidden className="size-4" /> Simulate a mis-share
          </Button>
          <Button
            variant="primary"
            loading={start.isPending}
            onClick={() => start.mutate({}, { onSuccess: (run) => onOpen(run.id) })}
            data-testid="proof-run"
          >
            <ShieldCheck aria-hidden className="size-4" /> Run leak test
          </Button>
        </div>
      </div>
      {start.error ? <ErrorNotice error={start.error} title="Could not start the leak test" /> : null}

      {latest ? (
        <LatestResult run={latest} onOpen={onOpen} />
      ) : firstPage.data?.total === 0 ? (
        <EmptyState
          icon={<ShieldCheck />}
          title="No leak test yet"
          description="Run one now, or wait for the weekly run."
        />
      ) : null}

      <section aria-labelledby="proof-history">
        <h2 id="proof-history" className="mb-3 text-sm font-semibold text-slate-900">
          History
        </h2>
        {runs.isError ? (
          <ErrorNotice error={runs.error} title="Could not load leak tests" onRetry={() => runs.refetch()} />
        ) : runs.isPending ? (
          <SkeletonRows rows={4} />
        ) : runs.data.items.length === 0 ? (
          <p className="text-sm text-slate-500">No runs yet.</p>
        ) : (
          <div className="space-y-4">
            <div className="relative overflow-x-auto rounded-lg border border-slate-200 bg-white">
              <table className="w-full min-w-[40rem] text-left text-sm" data-testid="proof-runs-table">
                <caption className="sr-only">Leak tests</caption>
                <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-600">
                  <tr>
                    <th scope="col" className="px-4 py-2 font-medium">
                      Requested
                    </th>
                    <th scope="col" className="px-4 py-2 font-medium">
                      Kind
                    </th>
                    <th scope="col" className="px-4 py-2 font-medium">
                      Result
                    </th>
                    <th scope="col" className="hidden px-4 py-2 text-right font-medium sm:table-cell">
                      Questions asked
                    </th>
                    <th scope="col" className="px-4 py-2">
                      <span className="sr-only">Report</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {runs.data.items.map((run) => (
                    <tr key={run.id} data-testid="proof-run-row" data-kind={run.kind}>
                      <td className="px-4 py-3 whitespace-nowrap text-slate-700">
                        <Time iso={run.created_at} />
                      </td>
                      <td className="px-4 py-3 text-slate-700">
                        <RunKind run={run} />
                      </td>
                      <td className="px-4 py-3">
                        <OutcomeBadge run={run} />
                      </td>
                      <td className="hidden px-4 py-3 text-right text-slate-700 tabular-nums sm:table-cell">
                        {run.status === "completed" ? formatNumber(num(run.summary as Summary, "probes_run")) : ""}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Button size="sm" variant="ghost" onClick={() => onOpen(run.id)}>
                          View report
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              page={runs.data.page}
              pageSize={runs.data.page_size}
              total={runs.data.total}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPage(1);
              }}
            />
          </div>
        )}
      </section>

      <SimulateDialog open={simulating} onOpenChange={setSimulating} onStarted={(run) => onOpen(run.id)} />
    </div>
  );
}

function LatestResult({ run, onOpen }: { run: ProofRun; onOpen: (id: string) => void }) {
  const { tone, label, text } = outcome(run);
  return (
    <Alert tone={tone} title={`Latest result: ${label}`} data-testid="proof-latest">
      <p>{text}</p>
      <p className="mt-1 text-xs opacity-80">
        <RunKind run={run} />, <Time iso={run.finished_at ?? run.created_at} />.{" "}
        <button type="button" className="font-medium underline" onClick={() => onOpen(run.id)}>
          View report
        </button>
      </p>
    </Alert>
  );
}

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-4 py-3">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-lg font-semibold text-slate-900 tabular-nums">{value}</dd>
    </div>
  );
}

function documentLabel(finding: ProofFinding) {
  if (finding.title) return finding.title;
  return finding.workspace?.name ? `A document in ${finding.workspace.name}` : "A deleted document";
}

function Report({ run }: { run: ProofRunDetail }) {
  const summary = run.summary as Summary;
  const documents = new Set(run.findings.map((f) => f.document_id)).size;
  const { tone, label, text } = outcome(run, documents);
  const done = run.status === "completed";
  const sampled = num(summary, "probes_run") < num(summary, "probes_planned");

  return (
    <div className="space-y-6" data-testid="proof-report" data-verdict={run.verdict ?? run.status}>
      <div>
        <h2 className="text-lg font-semibold text-slate-900">Leak test report</h2>
        <p className="mt-1 text-sm text-slate-600">
          <RunKind run={run} />
          {run.requested_by ? `, requested by ${run.requested_by.full_name ?? "a former member"}` : ""}. Started{" "}
          <Time iso={run.started_at ?? run.created_at} absolute />
          {run.finished_at ? (
            <>
              , finished <Time iso={run.finished_at} absolute />
            </>
          ) : null}
          .
        </p>
      </div>

      <Alert tone={tone} title={label} data-testid="proof-verdict">
        <p>{text}</p>
        {run.status === "queued" || run.status === "running" ? (
          <p className="mt-2 flex items-center gap-2 text-xs">
            <Spinner /> This page updates by itself.
          </p>
        ) : null}
      </Alert>

      {done ? (
        <>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Stat label="Access groups" value={formatNumber(num(summary, "groups"))} />
            <Stat label="Indexed documents" value={formatNumber(num(summary, "indexed_documents"))} />
            <Stat label="Questions asked" value={formatNumber(num(summary, "probes_run"))} />
            <Stat label="Passages checked" value={formatNumber(num(summary, "passages_checked"))} />
            <Stat label="Model requests checked" value={formatNumber(num(summary, "prompts_checked"))} />
            <Stat label="Took" value={formatMs(num(summary, "duration_ms"))} />
          </dl>
          {sampled ? (
            <Alert tone="warning" title="A sample, not every question">
              The run asked {formatNumber(num(summary, "probes_run"))} of {formatNumber(num(summary, "probes_planned"))}{" "}
              planned questions (the limit is {formatNumber(num(summary, "max_probes"))}), spread over every group and
              document. Raise PERMISSION_PROOF_MAX_PROBES to ask them all.
            </Alert>
          ) : null}

          <section aria-labelledby="proof-findings">
            <h3 id="proof-findings" className="mb-3 text-sm font-semibold text-slate-900">
              Findings
            </h3>
            {run.findings.length === 0 ? (
              <p className="text-sm text-slate-600" data-testid="proof-no-findings">
                None.
              </p>
            ) : (
              <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white print:overflow-visible">
                <table
                  className="w-full min-w-[36rem] text-left text-sm print:min-w-0"
                  data-testid="proof-findings-table"
                >
                  <caption className="sr-only">Findings</caption>
                  <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-600">
                    <tr>
                      <th scope="col" className="px-4 py-2 font-medium">
                        Group
                      </th>
                      <th scope="col" className="px-4 py-2 font-medium">
                        Document
                      </th>
                      <th scope="col" className="px-4 py-2 font-medium">
                        What happened
                      </th>
                      <th scope="col" className="px-4 py-2 text-right font-medium">
                        Times
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {run.findings.map((f) => (
                      <tr key={`${f.group}-${f.kind}-${f.document_id}`} data-testid="proof-finding-row">
                        <td className="px-4 py-2 text-slate-700">{f.group}</td>
                        <td className={cn("px-4 py-2", f.title ? "text-slate-900" : "text-slate-500 italic")}>
                          {documentLabel(f)}
                        </td>
                        <td className="px-4 py-2 text-slate-700">{FINDING_LABELS[f.kind]}</td>
                        <td className="px-4 py-2 text-right text-slate-700 tabular-nums">{formatNumber(f.count)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {run.findings.some((f) => !f.title) ? (
              <p className="mt-2 text-xs text-slate-500">
                Titles are shown only for documents you can read yourself; administrators read nothing by role.
              </p>
            ) : null}
          </section>

          <section aria-labelledby="proof-groups">
            <h3 id="proof-groups" className="mb-3 text-sm font-semibold text-slate-900">
              Access groups
            </h3>
            <p className="mb-3 text-sm text-slate-600">
              People with the same organization role and workspace roles who can read the same documents form one group.
              One person per group is asked; everyone&apos;s access is checked.
            </p>
            <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white print:overflow-visible">
              <table className="w-full min-w-[44rem] text-left text-sm print:min-w-0" data-testid="proof-groups-table">
                <caption className="sr-only">Access groups</caption>
                <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-600">
                  <tr>
                    <th scope="col" className="px-4 py-2 font-medium">
                      Group
                    </th>
                    <th scope="col" className="px-4 py-2 font-medium">
                      People
                    </th>
                    <th scope="col" className="px-4 py-2 font-medium">
                      Workspaces
                    </th>
                    <th scope="col" className="px-4 py-2 text-right font-medium">
                      Cannot read
                    </th>
                    <th scope="col" className="px-4 py-2 text-right font-medium">
                      Questions
                    </th>
                    <th scope="col" className="px-4 py-2 text-right font-medium">
                      Findings
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 align-top">
                  {run.groups.map((g) => (
                    <tr key={g.number} data-testid="proof-group-row">
                      <td className="px-4 py-2 text-slate-700">{g.number}</td>
                      <td className="px-4 py-2 text-slate-900">
                        {g.members.map((m) => m.full_name ?? "a former member").join(", ")}
                      </td>
                      <td className="px-4 py-2 text-slate-700">
                        {g.workspaces.length === 0 && !g.simulated_workspace ? (
                          <span className="text-slate-500">None</span>
                        ) : null}
                        {g.workspaces.map((w) => (
                          <span key={w.workspace_id} className="mr-2 inline-block">
                            {w.name ?? "a deleted workspace"} (
                            {WORKSPACE_ROLE_LABELS[w.role as keyof typeof WORKSPACE_ROLE_LABELS] ?? w.role})
                          </span>
                        ))}
                        {g.simulated_workspace ? (
                          <Badge tone="warning">Simulated: {g.simulated_workspace.name ?? "a deleted workspace"}</Badge>
                        ) : null}
                      </td>
                      <td className="px-4 py-2 text-right text-slate-700 tabular-nums">
                        {formatNumber(g.documents_unreadable)}
                      </td>
                      <td className="px-4 py-2 text-right text-slate-700 tabular-nums">{formatNumber(g.probes_run)}</td>
                      <td
                        className={cn(
                          "px-4 py-2 text-right tabular-nums",
                          g.findings ? "font-semibold text-red-700" : "text-slate-700",
                        )}
                      >
                        {formatNumber(g.findings)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section aria-labelledby="proof-method" className="text-sm text-slate-700">
            <h3 id="proof-method" className="mb-2 text-sm font-semibold text-slate-900">
              Method
            </h3>
            <ul className="list-disc space-y-1 pl-5">
              <li>
                Who may read what is worked out a second time, by code separate from the application&apos;s, from the
                memberships and shares at the start of the run. The application&apos;s answer must match it for every
                person.
              </li>
              <li>
                Each group is asked about every indexed document it cannot read, using the document&apos;s title and
                passages from it as the question. The search is the chat&apos;s own ({String(summary.retrieval_mode)},{" "}
                {formatNumber(num(summary, "candidates_per_probe"))} candidates, no reranking).
              </li>
              <li>
                Every passage found must come from a document the group can read, and the exact request that would go to
                the model must hold no run of {formatNumber(num(summary, "window_characters"))} characters found only in
                documents the group cannot read. No model is called. As in the chat, a question that finds nothing
                builds no model request.
              </li>
              <li>
                It does not show that the permissions themselves are right, and it covers the questions it asked, not
                every possible question.
              </li>
            </ul>
          </section>
        </>
      ) : null}
    </div>
  );
}

function RunView({ id, onBack }: { id: string; onBack: () => void }) {
  const run = useProofRun(id);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap justify-between gap-2 print:hidden">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeft aria-hidden className="size-4" /> All leak tests
        </Button>
        {run.data?.status === "completed" ? (
          <Button size="sm" onClick={() => window.print()}>
            <Printer aria-hidden className="size-4" /> Print or save as PDF
          </Button>
        ) : null}
      </div>
      {run.isError ? (
        <ErrorNotice error={run.error} title="Could not load the report" onRetry={() => run.refetch()} />
      ) : run.isPending ? (
        <SkeletonRows rows={6} />
      ) : (
        <Report run={run.data} />
      )}
    </div>
  );
}

export function PermissionProof() {
  const { params, update } = useUrlState();
  const runId = params.get("run");
  return runId ? (
    <RunView id={runId} onBack={() => update({ run: null })} />
  ) : (
    <Overview onOpen={(id) => update({ run: id })} />
  );
}
