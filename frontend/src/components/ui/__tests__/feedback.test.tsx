import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/errors";
import { ErrorNotice } from "../feedback";
import { Pagination } from "../pagination";

describe("ErrorNotice", () => {
  it("shows the message with the status and request id for support", () => {
    render(
      <ErrorNotice
        error={new ApiError(409, "conflict", "A workspace needs at least one owner", null, "req-123")}
        title="Could not update"
      />,
    );
    const notice = screen.getByRole("alert");
    expect(notice).toHaveTextContent("Could not update");
    expect(notice).toHaveTextContent("A workspace needs at least one owner");
    expect(notice).toHaveTextContent("HTTP 409");
    expect(notice).toHaveTextContent("Request ID: req-123");
    expect(screen.getByRole("button", { name: "Copy request ID" })).toBeInTheDocument();
  });

  it("explains rate limits using Retry-After and permission errors plainly", () => {
    const { rerender } = render(
      <ErrorNotice error={new ApiError(429, "rate_limited", "Too many requests, slow down", null, null, 30)} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Too many requests. Please try again in 30 seconds.");
    rerender(<ErrorNotice error={new ApiError(403, "forbidden", "You do not have permission to do this")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("You do not have permission to do this");
  });

  it("offers a retry action only when given one", async () => {
    const onRetry = vi.fn();
    const { rerender } = render(<ErrorNotice error={new Error("boom")} />);
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    rerender(<ErrorNotice error={new Error("boom")} onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalled();
  });
});

describe("Pagination", () => {
  it("reports the visible range and moves between pages", async () => {
    const onPageChange = vi.fn();
    render(<Pagination page={2} pageSize={20} total={87} onPageChange={onPageChange} />);
    expect(screen.getByText("21-40 of 87")).toBeInTheDocument();
    expect(screen.getByText("Page 2 of 5")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(onPageChange).toHaveBeenLastCalledWith(3);
    await userEvent.click(screen.getByRole("button", { name: "Previous page" }));
    expect(onPageChange).toHaveBeenLastCalledWith(1);
  });

  it("disables the buttons at both ends and handles an empty list", () => {
    const { rerender } = render(<Pagination page={1} pageSize={20} total={5} onPageChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    rerender(<Pagination page={1} pageSize={20} total={0} onPageChange={() => {}} />);
    expect(screen.getByText("No results")).toBeInTheDocument();
  });

  it("lets the user change the page size", async () => {
    const onPageSizeChange = vi.fn();
    render(
      <Pagination page={1} pageSize={20} total={200} onPageChange={() => {}} onPageSizeChange={onPageSizeChange} />,
    );
    await userEvent.selectOptions(screen.getByRole("combobox"), "50");
    expect(onPageSizeChange).toHaveBeenCalledWith(50);
  });
});
