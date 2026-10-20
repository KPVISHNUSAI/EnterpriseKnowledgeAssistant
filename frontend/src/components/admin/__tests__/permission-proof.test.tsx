import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProofRun, ProofRunDetail } from "@/lib/api/types";
import { makeWorkspace } from "@/test/fixtures";
import { nav } from "@/test/navigation";
import { errorResponse, jsonResponse, renderWithProviders } from "@/test/render";
import { PermissionProof } from "../permission-proof";

const RUNS = "/api/v1/admin/permission-proof/runs";
const board = makeWorkspace({ name: "Board" });
const engineering = makeWorkspace({ name: "Engineering" });
const contractor = {
  user_id: "user-contractor",
  email: "cara@acme.test",
  full_name: "Cara Contractor",
  role: "employee",
  joined_at: "2026-01-01T00:00:00Z",
  is_active: true,
};

const passedSummary = {
  groups: 5,
  members: 9,
  documents: 12,
  indexed_documents: 12,
  probes_planned: 114,
  probes_run: 114,
  prompts_checked: 63,
  passages_checked: 1656,
  findings: 0,
  max_probes: 5000,
  retrieval_mode: "hybrid",
  candidates_per_probe: 20,
  window_characters: 50,
  duration_ms: 2200,
};

function makeRun(overrides: Partial<ProofRun> = {}): ProofRun {
  return {
    id: "run-1",
    kind: "manual",
    status: "completed",
    verdict: "passed",
    requested_by: { user_id: "user-admin", full_name: "Ada Admin", email: "ada@acme.test" },
    simulation: null,
    error: null,
    summary: passedSummary,
    created_at: "2026-10-19T02:00:00Z",
    started_at: "2026-10-19T02:00:01Z",
    finished_at: "2026-10-19T02:00:03Z",
    ...overrides,
  };
}

const simulation = {
  person: { user_id: contractor.user_id, full_name: contractor.full_name, email: contractor.email },
  workspace: { workspace_id: board.id, name: "Board" },
};

function makeDetail(overrides: Partial<ProofRunDetail> = {}): ProofRunDetail {
  return {
    ...makeRun(),
    groups: [
      {
        number: 1,
        members: [{ user_id: "user-eng", full_name: "Eli Engineer", email: "eli@acme.test" }],
        representative: { user_id: "user-eng", full_name: "Eli Engineer", email: "eli@acme.test" },
        workspaces: [{ workspace_id: engineering.id, name: "Engineering", role: "editor" }],
        simulated_workspace: null,
        readable_documents: 4,
        documents_unreadable: 8,
        documents_probed: 8,
        probes_planned: 24,
        probes_run: 24,
        prompts_checked: 13,
        passages_checked: 260,
        findings: 0,
      },
    ],
    findings: [],
    ...overrides,
  };
}

const fetchMock = vi.fn();
type Route = (url: URL, init: RequestInit) => Response | Promise<Response> | undefined;
let routes: Route[] = [];

beforeEach(() => {
  routes = [];
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input: string, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://localhost");
    for (const route of routes) {
      const response = route(url, init);
      if (response) return response;
    }
    if (url.pathname === "/api/v1/workspaces") return jsonResponse([board, engineering]);
    if (url.pathname === "/api/v1/organization/members")
      return jsonResponse({ items: [contractor], total: 1, page: 1, page_size: 20 });
    throw new Error(`unexpected request ${init.method ?? "GET"} ${url.pathname}`);
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

function listing(items: ProofRun[]) {
  routes.push((url, init) =>
    url.pathname === RUNS && (init.method ?? "GET") === "GET"
      ? jsonResponse({ items, total: items.length, page: 1, page_size: 20 })
      : undefined,
  );
}

function detail(run: ProofRunDetail) {
  routes.push((url) => (url.pathname === `${RUNS}/${run.id}` ? jsonResponse(run) : undefined));
}

function posts() {
  return fetchMock.mock.calls
    .filter(([input, init]) => String(input).startsWith(RUNS) && init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init.body)));
}

describe("PermissionProof overview", () => {
  it("shows the latest real result, never a simulation, and the history", async () => {
    listing([
      makeRun({ id: "sim-1", kind: "simulation", verdict: "failed", simulation, created_at: "2026-10-20T09:00:00Z" }),
      makeRun(),
    ]);
    renderWithProviders(<PermissionProof />);

    const latest = await screen.findByTestId("proof-latest");
    expect(latest).toHaveTextContent("Latest result: Passed");
    expect(latest).toHaveTextContent(
      "No leak found in 114 questions asked as 5 access groups: nothing found or sent to the model came from a document the group cannot read.",
    );
    const rows = screen.getAllByTestId("proof-run-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Simulation: Cara Contractor in Board");
    expect(within(rows[0]!).getByTestId("proof-outcome")).toHaveTextContent("Caught");
    expect(within(rows[1]!).getByTestId("proof-outcome")).toHaveTextContent("Passed");
  });

  it("explains when no test has run yet", async () => {
    listing([]);
    renderWithProviders(<PermissionProof />);
    expect(await screen.findByText("No leak test yet")).toBeInTheDocument();
  });

  it("starts a run and opens its report", async () => {
    const user = userEvent.setup();
    listing([]);
    routes.push((url, init) =>
      url.pathname === RUNS && init.method === "POST"
        ? jsonResponse(makeDetail({ id: "run-new", status: "queued", verdict: null, summary: {} }), { status: 202 })
        : undefined,
    );
    renderWithProviders(<PermissionProof />);
    await user.click(await screen.findByTestId("proof-run"));

    await waitFor(() => expect(window.location.search).toBe("?run=run-new"));
    expect(posts()).toEqual([{}]);
  });

  it("shows why a run could not start", async () => {
    const user = userEvent.setup();
    listing([]);
    routes.push((url, init) =>
      url.pathname === RUNS && init.method === "POST"
        ? errorResponse(409, "proof_running", "A leak test is already running for this organization.")
        : undefined,
    );
    renderWithProviders(<PermissionProof />);
    await user.click(await screen.findByTestId("proof-run"));

    expect(await screen.findByText("A leak test is already running for this organization.")).toBeInTheDocument();
    expect(window.location.search).toBe("");
  });

  it("simulates a mis-share for a chosen person and workspace", async () => {
    const user = userEvent.setup();
    listing([]);
    routes.push((url, init) =>
      url.pathname === RUNS && init.method === "POST"
        ? jsonResponse(makeDetail({ id: "sim-new", kind: "simulation", status: "queued", verdict: null }), {
            status: 202,
          })
        : undefined,
    );
    renderWithProviders(<PermissionProof />);
    await user.click(await screen.findByTestId("proof-simulate-open"));
    const dialog = await screen.findByRole("dialog", { name: "Simulate a mis-share" });
    expect(within(dialog).getByTestId("proof-simulate-submit")).toBeDisabled();

    await user.click(within(dialog).getByTestId("proof-person"));
    await user.click(await screen.findByRole("button", { name: /Cara Contractor/ }));
    await waitFor(() => expect(within(dialog).getByRole("option", { name: "Board" })).toBeInTheDocument());
    await user.selectOptions(within(dialog).getByTestId("proof-workspace"), "Board");
    await user.click(within(dialog).getByTestId("proof-simulate-submit"));

    await waitFor(() => expect(window.location.search).toBe("?run=sim-new"));
    expect(posts()).toEqual([{ simulate: { user_id: contractor.user_id, workspace_id: board.id } }]);
  });

  it("keeps the dialog open with the reason a simulation cannot run", async () => {
    const user = userEvent.setup();
    listing([]);
    routes.push((url, init) =>
      url.pathname === RUNS && init.method === "POST"
        ? errorResponse(422, "proof_already_member", "Cara Contractor already belongs to Board.")
        : undefined,
    );
    renderWithProviders(<PermissionProof />);
    await user.click(await screen.findByTestId("proof-simulate-open"));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByTestId("proof-person"));
    await user.click(await screen.findByRole("button", { name: /Cara Contractor/ }));
    await waitFor(() => expect(within(dialog).getByRole("option", { name: "Board" })).toBeInTheDocument());
    await user.selectOptions(within(dialog).getByTestId("proof-workspace"), "Board");
    await user.click(within(dialog).getByTestId("proof-simulate-submit"));

    expect(await within(dialog).findByText("Cara Contractor already belongs to Board.")).toBeInTheDocument();
    expect(window.location.search).toBe("");
  });
});

describe("PermissionProof report", () => {
  beforeEach(() => {
    nav.search = new URLSearchParams("run=run-1");
  });

  it("reports a pass with its counts, groups and method", async () => {
    detail(makeDetail());
    renderWithProviders(<PermissionProof />);

    const report = await screen.findByTestId("proof-report");
    expect(report).toHaveAttribute("data-verdict", "passed");
    expect(screen.getByTestId("proof-verdict")).toHaveTextContent("Passed");
    expect(screen.getByTestId("proof-no-findings")).toBeInTheDocument();
    const group = screen.getByTestId("proof-group-row");
    expect(group).toHaveTextContent("Eli Engineer");
    expect(group).toHaveTextContent("Engineering (Editor)");
    expect(screen.getByText(/no run of 50 characters/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Print or save as PDF/ })).toBeInTheDocument();
    expect(screen.queryByText(/A sample, not every question/)).not.toBeInTheDocument();
  });

  it("names a document only when the viewer may read it", async () => {
    detail(
      makeDetail({
        verdict: "failed",
        summary: { ...passedSummary, findings: 2 },
        findings: [
          {
            group: 1,
            kind: "prompt_text",
            document_id: "doc-board",
            title: null,
            workspace: { workspace_id: board.id, name: "Board" },
            count: 3,
          },
          {
            group: 1,
            kind: "retrieved",
            document_id: "doc-eng",
            title: "Release checklist",
            workspace: { workspace_id: engineering.id, name: "Engineering" },
            count: 1,
          },
        ],
      }),
    );
    renderWithProviders(<PermissionProof />);

    expect(await screen.findByTestId("proof-verdict")).toHaveTextContent("Failed");
    expect(screen.getByTestId("proof-verdict")).toHaveTextContent("2 findings about 2 documents");
    const [hidden, named] = screen.getAllByTestId("proof-finding-row");
    expect(hidden).toHaveTextContent("A document in Board");
    expect(hidden).toHaveTextContent("Its text was in the model request");
    expect(named).toHaveTextContent("Release checklist");
    expect(screen.getByText(/Titles are shown only for documents you can read yourself/)).toBeInTheDocument();
  });

  it("calls a caught simulation a success and an uncaught one a failure", async () => {
    detail(
      makeDetail({
        kind: "simulation",
        simulation,
        verdict: "failed",
        summary: { ...passedSummary, findings: 30 },
        findings: [
          {
            group: 1,
            kind: "access_extra",
            document_id: "doc-board",
            title: null,
            workspace: { workspace_id: board.id, name: "Board" },
            count: 1,
          },
        ],
      }),
    );
    const { unmount } = renderWithProviders(<PermissionProof />);
    const verdict = await screen.findByTestId("proof-verdict");
    expect(verdict).toHaveTextContent("Caught");
    expect(verdict).toHaveTextContent(
      "The test reported 30 findings about 1 document for Cara Contractor, who was treated as wrongly given Board.",
    );
    unmount();

    routes = [];
    detail(makeDetail({ kind: "simulation", simulation, verdict: "passed" }));
    renderWithProviders(<PermissionProof />);
    expect(await screen.findByTestId("proof-verdict")).toHaveTextContent("Not caught");
  });

  it("says when the run asked a sample of the questions", async () => {
    detail(makeDetail({ summary: { ...passedSummary, probes_run: 5000, probes_planned: 7200 } }));
    renderWithProviders(<PermissionProof />);
    expect(await screen.findByText("A sample, not every question")).toBeInTheDocument();
    expect(screen.getByText(/asked 5,000 of 7,200/)).toBeInTheDocument();
  });

  it("shows a queued run as waiting, without a report to print", async () => {
    detail(
      makeDetail({ status: "queued", verdict: null, summary: {}, groups: [], started_at: null, finished_at: null }),
    );
    renderWithProviders(<PermissionProof />);
    expect(await screen.findByTestId("proof-verdict")).toHaveTextContent("Queued");
    expect(screen.getByText("This page updates by itself.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Print/ })).not.toBeInTheDocument();
  });

  it("explains an interrupted run", async () => {
    detail(makeDetail({ status: "interrupted", verdict: null, summary: {} }));
    renderWithProviders(<PermissionProof />);
    expect(await screen.findByTestId("proof-verdict")).toHaveTextContent("Interrupted");
  });
});

describe("PermissionProof review fixes", () => {
  async function fillSimulation(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByTestId("proof-simulate-open"));
    const dialog = await screen.findByRole("dialog", { name: "Simulate a mis-share" });
    await user.click(within(dialog).getByRole("button", { name: "Person Choose a person" }));
    await user.click(await screen.findByRole("button", { name: /Cara Contractor/ }));
    expect(within(dialog).getByRole("button", { name: /^Person Cara Contractor/ })).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByRole("option", { name: "Board" })).toBeInTheDocument());
    await user.selectOptions(within(dialog).getByTestId("proof-workspace"), "Board");
    return dialog;
  }

  it("cannot close the dialog while the simulation is being started", async () => {
    const user = userEvent.setup();
    listing([]);
    routes.push((url, init) =>
      url.pathname === RUNS && init.method === "POST" ? new Promise<Response>(() => {}) : undefined,
    );
    renderWithProviders(<PermissionProof />);
    const dialog = await fillSimulation(user);
    await user.click(within(dialog).getByTestId("proof-simulate-submit"));

    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled());
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Simulate a mis-share" })).toBeInTheDocument();
  });

  it("says when a passing run asked only a sample, on the overview too", async () => {
    listing([makeRun({ summary: { ...passedSummary, probes_run: 5000, probes_planned: 7200 } })]);
    renderWithProviders(<PermissionProof />);
    const latest = await screen.findByTestId("proof-latest");
    expect(latest).toHaveTextContent("Latest result: Passed (sample)");
    expect(latest).toHaveTextContent("The questions were a sample of the 7,200 planned.");
    expect(screen.getByTestId("proof-outcome")).toHaveTextContent("Passed (sample)");
  });

  it("does not claim there was no test when the first page holds only simulations", async () => {
    listing([makeRun({ kind: "simulation", verdict: "failed", simulation })]);
    renderWithProviders(<PermissionProof />);
    expect(await screen.findByTestId("proof-run-row")).toBeInTheDocument();
    expect(screen.queryByTestId("proof-latest")).not.toBeInTheDocument();
    expect(screen.queryByText("No leak test yet")).not.toBeInTheDocument();
  });

  it("shows a simulation with nothing left to test as neutral, not as a failure of the test", async () => {
    nav.search = new URLSearchParams("run=run-1");
    detail(makeDetail({ kind: "simulation", simulation, verdict: "nothing_to_test" }));
    renderWithProviders(<PermissionProof />);
    const verdict = await screen.findByTestId("proof-verdict");
    expect(verdict).toHaveTextContent("Nothing to test");
    expect(verdict).not.toHaveTextContent("Not caught");
  });
});
