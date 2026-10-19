import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EMPTY_SCOPE } from "@/components/shared/scope-picker";
import { StreamUnavailableError, streamChat, type ChatStreamEvent } from "@/lib/api/chat-stream";
import { ApiError } from "@/lib/api/errors";
import type { Message } from "@/lib/api/types";
import { makeSource } from "@/test/fixtures";
import { jsonResponse, makeClient } from "@/test/render";
import { isBusy, turnBelongsTo, useChatStream, type LiveTurn } from "../chat-stream";

vi.mock("@/lib/api/chat-stream", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/chat-stream")>()),
  streamChat: vi.fn(),
}));

const mockedStream = vi.mocked(streamChat);
const fetchMock = vi.fn();

const message = (overrides: Partial<Message> = {}): Message => ({
  id: "a1",
  conversation_id: "c1",
  role: "assistant",
  content: "Hello world [S1]",
  status: "complete",
  answer_status: "answered",
  sources: [makeSource("S1")],
  citations: [],
  metadata: {},
  redacted: false,
  created_at: "2026-01-01T00:00:00Z",
  ...overrides,
});

const events = (...list: ChatStreamEvent[]) =>
  async function* () {
    yield* list;
  };

const meta = (conversationId = "c1"): ChatStreamEvent => ({
  event: "meta",
  data: { conversation_id: conversationId, user_message_id: "u1", assistant_message_id: "a1" },
});

function setup(conversationId?: string, onCreated = vi.fn()) {
  const client = makeClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(
    () => useChatStream({ conversationId, scope: EMPTY_SCOPE, onConversationCreated: onCreated }),
    { wrapper },
  );
  return { ...hook, onCreated, client };
}

beforeEach(() => {
  mockedStream.mockReset();
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(jsonResponse({ items: [], total: 0, page: 1, page_size: 20 }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("useChatStream", () => {
  it("folds meta, sources, tokens and done into one live turn", async () => {
    const final = message();
    mockedStream.mockImplementation(
      events(
        meta(),
        { event: "sources", data: { sources: [makeSource("S1")] } },
        { event: "token", data: { text: "Hello " } },
        { event: "token", data: { text: "world [S1]" } },
        { event: "done", data: { message: final } },
      ),
    );
    const { result, onCreated } = setup();

    await act(async () => {
      await result.current.send("What is up?");
    });

    const live = result.current.live!;
    expect(live).toMatchObject({
      phase: "done",
      userText: "What is up?",
      text: "Hello world [S1]",
      createdId: "c1",
      assistantMessageId: "a1",
      message: final,
    });
    expect(live.sources).toHaveLength(1);
    expect(result.current.isStreaming).toBe(false);
    expect(onCreated).toHaveBeenCalledTimes(1);
    expect(onCreated).toHaveBeenCalledWith("c1");
  });

  it("sends the question with the conversation and the chosen scope", async () => {
    mockedStream.mockImplementation(events(meta("c9"), { event: "done", data: { message: message() } }));
    const client = makeClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const scope = { workspaceIds: ["w1"], documents: [{ id: "d1", title: "Doc" }] };
    const onCreated = vi.fn();
    const { result } = renderHook(
      () => useChatStream({ conversationId: "c9", scope, onConversationCreated: onCreated }),
      { wrapper },
    );

    await act(async () => {
      await result.current.send("Follow-up");
    });
    expect(mockedStream.mock.calls[0]?.[0]).toEqual({
      message: "Follow-up",
      conversation_id: "c9",
      scope: { workspace_ids: ["w1"], document_ids: ["d1"] },
    });
    expect(onCreated).not.toHaveBeenCalled(); // not a new conversation
  });

  it("keeps the partial text and the server's message when the stream reports an error", async () => {
    mockedStream.mockImplementation(
      events(
        meta(),
        { event: "token", data: { text: "Partial " } },
        { event: "error", data: { code: "provider_error", message: "The language model request failed" } },
      ),
    );
    const { result } = setup();
    await act(async () => {
      await result.current.send("q");
    });
    expect(result.current.live).toMatchObject({
      phase: "error",
      text: "Partial ",
      error: { message: "The language model request failed" },
    });
  });

  it("shows HTTP errors raised before the stream (with Retry-After and request id)", async () => {
    mockedStream.mockImplementation(async function* () {
      throw new ApiError(429, "rate_limited", "Too many requests, slow down", null, "req-429", 17);
      yield* [] as ChatStreamEvent[];
    });
    const { result } = setup();
    await act(async () => {
      await result.current.send("q");
    });
    expect(result.current.live).toMatchObject({
      phase: "error",
      error: { message: "Too many requests. Please try again in 17 seconds.", requestId: "req-429" },
    });
  });

  it("stops generating on request and keeps what was written", async () => {
    mockedStream.mockImplementation(async function* (_body, signal) {
      yield meta();
      yield { event: "token", data: { text: "Writing" } } as ChatStreamEvent;
      await new Promise((_, reject) =>
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))),
      );
    });
    const { result } = setup();
    let done!: Promise<void>;
    act(() => {
      done = result.current.send("long question");
    });
    await waitFor(() => expect(result.current.live?.text).toBe("Writing"));
    expect(result.current.isStreaming).toBe(true);

    act(() => result.current.stop());
    await act(async () => {
      await done;
    });
    expect(result.current.live).toMatchObject({ phase: "stopped", text: "Writing" });
    expect(result.current.isStreaming).toBe(false);
  });

  it("ignores a second question while one is being answered", async () => {
    mockedStream.mockImplementation(async function* (_body, signal) {
      yield meta();
      await new Promise((_, reject) =>
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))),
      );
    });
    const { result } = setup();
    let first!: Promise<void>;
    act(() => {
      first = result.current.send("one");
    });
    await act(async () => {
      await result.current.send("two");
    });
    expect(mockedStream).toHaveBeenCalledTimes(1);
    act(() => result.current.stop());
    await act(async () => {
      await first;
    });
  });

  it("falls back to the non-streaming endpoint when the response is not an event stream", async () => {
    mockedStream.mockImplementation(async function* () {
      throw new StreamUnavailableError();
      yield* [] as ChatStreamEvent[];
    });
    fetchMock.mockImplementation(async (url: string) =>
      url === "/api/v1/chat"
        ? jsonResponse({
            conversation_id: "c5",
            user_message: message({ id: "u5", role: "user" }),
            assistant_message: message({ id: "a5", content: "Plain answer" }),
          })
        : jsonResponse({ items: [], total: 0, page: 1, page_size: 20 }),
    );
    const { result, onCreated } = setup();
    await act(async () => {
      await result.current.send("hi");
    });
    expect(result.current.live).toMatchObject({
      phase: "done",
      text: "Plain answer",
      createdId: "c5",
      assistantMessageId: "a5",
    });
    expect(onCreated).toHaveBeenCalledWith("c5");
  });
});

describe("turnBelongsTo", () => {
  const turn = (overrides: Partial<LiveTurn>): LiveTurn => ({
    startedIn: null,
    createdId: null,
    userMessageId: null,
    assistantMessageId: null,
    userText: "q",
    text: "",
    sources: [],
    phase: "retrieving",
    message: null,
    error: null,
    ...overrides,
  });

  it("belongs to the new-chat screen until the server assigns an id, then to that conversation", () => {
    expect(turnBelongsTo(turn({}), undefined)).toBe(true);
    expect(turnBelongsTo(turn({ createdId: "c1" }), undefined)).toBe(true); // URL not updated yet
    expect(turnBelongsTo(turn({ createdId: "c1" }), "c1")).toBe(true);
    expect(turnBelongsTo(turn({ createdId: "c1" }), "c2")).toBe(false);
  });

  it("belongs to the conversation it was sent in", () => {
    expect(turnBelongsTo(turn({ startedIn: "c1" }), "c1")).toBe(true);
    expect(turnBelongsTo(turn({ startedIn: "c1" }), "c2")).toBe(false);
    expect(turnBelongsTo(turn({ startedIn: "c1" }), undefined)).toBe(false);
  });

  it("knows when it is busy", () => {
    expect(isBusy(turn({ phase: "retrieving" }))).toBe(true);
    expect(isBusy(turn({ phase: "generating" }))).toBe(true);
    expect(isBusy(turn({ phase: "done" }))).toBe(false);
    expect(isBusy(null)).toBe(false);
  });
});
