import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "../status-badge";

describe("StatusBadge", () => {
  it.each([
    ["queued", "Queued"],
    ["processing", "Processing"],
    ["indexed", "Indexed"],
    ["failed", "Failed"],
  ] as const)("shows %s as %s and exposes the raw status for tests", (status, label) => {
    render(<StatusBadge status={status} />);
    const badge = screen.getByTestId("document-status");
    expect(badge).toHaveTextContent(label);
    expect(badge).toHaveAttribute("data-status", status);
  });

  it("offers the failure reason as a tooltip, only for failures", () => {
    const { rerender } = render(<StatusBadge status="failed" error="PDF is password protected" />);
    expect(screen.getByTestId("document-status")).toHaveAttribute("title", "PDF is password protected");
    rerender(<StatusBadge status="indexed" error="stale error" />);
    expect(screen.getByTestId("document-status")).not.toHaveAttribute("title");
  });
});
