import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DisplayMessage } from "@/lib/chat-state";
import { AnswerBanner, answerNotice } from "../answer-banner";

const message = (overrides: Partial<DisplayMessage>): DisplayMessage => ({
  id: "m1",
  role: "assistant",
  content: "text",
  status: "complete",
  answerStatus: "answered",
  sources: [],
  citations: [],
  ...overrides,
});

describe("answerNotice", () => {
  it("shows nothing for a normal answer", () => {
    expect(answerNotice(message({}))).toBeNull();
  });

  it.each([
    ["insufficient_evidence", "Not enough evidence", "top"],
    ["partial", "Partial answer", "top"],
    ["unverified", "Unverified answer", "top"],
    ["refused", "declined to answer", "top"],
    ["error", "Something went wrong", "bottom"],
  ] as const)("explains answer_status %s", (answerStatus, title, placement) => {
    const notice = answerNotice(message({ answerStatus }));
    expect(notice?.title).toContain(title);
    expect(notice?.placement).toBe(placement);
    expect(notice?.status).toBe(answerStatus);
  });

  it("explains a stopped (incomplete) answer after the text", () => {
    expect(answerNotice(message({ status: "incomplete", answerStatus: null }))).toMatchObject({
      title: "Generation stopped",
      placement: "bottom",
    });
  });

  it("prefers the live error, which carries the server's message and request id", () => {
    const notice = answerNotice(
      message({
        error: { message: "Too many requests. Please try again in 17 seconds.", requestId: "req-9" },
        answerStatus: null,
      }),
    );
    expect(notice).toMatchObject({
      tone: "danger",
      body: "Too many requests. Please try again in 17 seconds.",
      requestId: "req-9",
    });
  });

  it("renders as an alert for errors with the request id visible", () => {
    const notice = answerNotice(message({ error: { message: "Boom", requestId: "req-9" } }))!;
    render(<AnswerBanner notice={notice} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Boom");
    expect(screen.getByTestId("answer-banner")).toHaveAttribute("data-status", "error");
    expect(screen.getByText("req-9")).toBeInTheDocument();
  });
});
