import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CSRF_COOKIE } from "../api/client";
import { StreamInterruptedError, StreamUnavailableError, streamChat, type ChatStreamEvent } from "../api/chat-stream";
import { ApiError } from "../api/errors";
import { errorResponse, jsonResponse } from "@/test/render";

const fetchMock = vi.fn();
const encoder = new TextEncoder();

function sseResponse(
  chunks: string[],
  { keepOpen = false, signal }: { keepOpen?: boolean; signal?: AbortSignal } = {},
) {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      if (!keepOpen) controller.close();
      signal?.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")));
    },
  });
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

async function collect(stream: AsyncGenerator<ChatStreamEvent>) {
  const events: ChatStreamEvent[] = [];
  for await (const event of stream) events.push(event);
  return events;
}

const wire = [
  'event: meta\ndata: {"conversation_id":"c1","user_message_id":"u1","assistant_message_id":"a1"}\n\n',
  ": keep-alive\n\n",
  'event: sources\ndata: {"sources":[]}\n\n',
  'event: token\ndata: {"text":"Hel"}\n\n',
  'event: token\ndata: {"text":"lo"}\n\n',
  "event: unknown_future_event\ndata: {}\n\n",
  'event: done\ndata: {"message":{"id":"a1"}}\n\n',
].join("");

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("streamChat", () => {
  it("yields the known events in order and skips heartbeats and unknown events", async () => {
    fetchMock.mockResolvedValue(sseResponse([wire]));
    const events = await collect(streamChat({ message: "hi" }, new AbortController().signal));
    expect(events.map((e) => e.event)).toEqual(["meta", "sources", "token", "token", "done"]);
    expect(events[2]).toEqual({ event: "token", data: { text: "Hel" } });
  });

  it("reassembles events split at arbitrary byte boundaries", async () => {
    const bytes = encoder.encode(wire);
    const chunks: string[] = [];
    const decoder = new TextDecoder();
    for (let i = 0; i < bytes.length; i += 5) chunks.push(decoder.decode(bytes.slice(i, i + 5), { stream: true }));
    fetchMock.mockResolvedValue(sseResponse(chunks));
    const events = await collect(streamChat({ message: "hi" }, new AbortController().signal));
    expect(events.filter((e) => e.event === "token").map((e) => (e.data as { text: string }).text)).toEqual([
      "Hel",
      "lo",
    ]);
  });

  it("POSTs JSON with the CSRF header and asks for an event stream", async () => {
    document.cookie = `${CSRF_COOKIE}=csrf-123; path=/`;
    fetchMock.mockResolvedValue(sseResponse([wire]));
    await collect(streamChat({ message: "hi", conversation_id: "c1" }, new AbortController().signal));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/chat/stream");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ message: "hi", conversation_id: "c1" }));
    expect(init.headers).toMatchObject({ Accept: "text/event-stream", "X-CSRF-Token": "csrf-123" });
  });

  it("throws the API error envelope for HTTP errors raised before the stream starts", async () => {
    fetchMock.mockResolvedValue(errorResponse(404, "not_found", "Not found", { requestId: "req-42" }));
    const err = await collect(streamChat({ message: "hi" }, new AbortController().signal)).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 404, code: "not_found", requestId: "req-42" });
  });

  it("reports a response that is not an event stream so the caller can fall back", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    await expect(collect(streamChat({ message: "hi" }, new AbortController().signal))).rejects.toBeInstanceOf(
      StreamUnavailableError,
    );
  });

  it("flags a stream that ends without a terminal event", async () => {
    fetchMock.mockResolvedValue(sseResponse(['event: token\ndata: {"text":"partial"}\n\n']));
    const seen: string[] = [];
    const run = async () => {
      for await (const event of streamChat({ message: "hi" }, new AbortController().signal)) seen.push(event.event);
    };
    await expect(run()).rejects.toBeInstanceOf(StreamInterruptedError);
    expect(seen).toEqual(["token"]);
  });

  it("surfaces an abort as an AbortError", async () => {
    const abort = new AbortController();
    fetchMock.mockResolvedValue(
      sseResponse(['event: token\ndata: {"text":"a"}\n\n'], { keepOpen: true, signal: abort.signal }),
    );
    const run = async () => {
      for await (const event of streamChat({ message: "hi" }, abort.signal)) {
        if (event.event === "token") abort.abort();
      }
    };
    await expect(run()).rejects.toMatchObject({ name: "AbortError" });
  });

  it("maps a network failure to a network_error", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(collect(streamChat({ message: "hi" }, new AbortController().signal))).rejects.toMatchObject({
      code: "network_error",
    });
  });
});
