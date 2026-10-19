import { makePdf } from "./pdf";

/** A file as Playwright's setInputFiles and the upload API take it. All content is invented. */
export interface SampleFile {
  name: string;
  mimeType: string;
  buffer: Buffer;
}

const text = (name: string, mimeType: string, content: string): SampleFile => ({
  name,
  mimeType,
  buffer: Buffer.from(content.trim() + "\n", "utf8"),
});

export const remoteWorkPolicy = text(
  "Remote Work Policy.md",
  "text/markdown",
  `
# Remote Work Policy

## Eligibility

Employees may work remotely up to three days per week with manager approval. New hires become eligible after their ninety day probation period.

## Approval

Remote work requests are approved by the direct manager. Managers review every request within five business days.

## Security

Remote employees must connect through the corporate VPN and enable full disk encryption on every device.
`,
);

export const travelPolicy = text(
  "Travel Policy.txt",
  "text/plain",
  `
Travel Policy

Employees book economy class flights for trips shorter than six hours.

Hotel stays are capped at two hundred dollars per night in major cities and require receipts for reimbursement.

Rental cars are permitted only when public transport is unavailable.
`,
);

/** Three pages with one bookmark each, so every page is its own chunk (see support/pdf.ts). */
export const safetyManual: SampleFile = {
  name: "Facility Safety Manual.pdf",
  mimeType: "application/pdf",
  buffer: makePdf([
    {
      heading: "Fire Safety",
      lines: [
        "Sprinkler heads are inspected every quarter by the facilities team.",
        "Fire extinguishers are mounted beside each stairwell and checked monthly.",
        "Staff must never prop open a fire door.",
      ],
    },
    {
      heading: "Chemical Handling",
      lines: [
        "Open solvent containers only inside a certified fume hood.",
        "Store acids and bases in separate labelled cabinets.",
        "Report every spill to the safety officer immediately.",
      ],
    },
    {
      heading: "Emergency Evacuation",
      lines: [
        "Staff gather at the assembly point behind building C.",
        "The floor warden confirms the headcount before anyone re-enters.",
        "Elevators are never used during an evacuation.",
      ],
    },
  ]),
};

/** A different text than the travel policy, for replacing a file (the version must increment). */
export const travelPolicyV2 = text(
  "Travel Policy v2.txt",
  "text/plain",
  `
Travel Policy

Employees book economy class flights for trips shorter than eight hours.

Train travel is preferred for journeys under four hundred kilometres.
`,
);

/** Visible to the whole workspace in the permission tests. */
export const engineeringHandbook = text(
  "Engineering Handbook.md",
  "text/markdown",
  `
# Engineering Handbook

New engineers receive a laptop, repository access and a security briefing during their first week.

Code reviews require two approvals before a change is merged.
`,
);

/** Restricted in the permission tests; "zephyrquartz" appears nowhere else. */
export const compensationMemo = text(
  "Executive Compensation Memo.md",
  "text/markdown",
  `
# Executive Compensation Memo

The executive bonus pool for the coming year is the zephyrquartz allocation of four million credits, divided between the leadership team.
`,
);

/** Three long sentences that all answer "What is the incident escalation process?": a long answer to stop. */
export const incidentRunbook = text(
  "Incident Escalation Runbook.md",
  "text/markdown",
  `
# Incident Escalation Runbook

The on-call engineer receives the incident alert from the monitoring system, acknowledges the escalation within five minutes and starts a dedicated incident channel for the response team.

If the incident remains unresolved after thirty minutes, the escalation process automatically notifies the engineering manager, who then decides whether additional specialists must join the response.

The engineering manager coordinates the incident escalation process with the communications team, keeps stakeholders informed every hour and documents everything in a written review afterwards.
`,
);
