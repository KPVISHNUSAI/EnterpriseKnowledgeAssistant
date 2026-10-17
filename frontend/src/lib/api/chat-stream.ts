import { apiUrl, csrfHeaders, errorFromResponse } from "./client";
import { isAbortError, networkError } from "./errors";
import { SseParser } from "../sse";
import type { ChatRequest, StreamDone, StreamError, StreamMeta, StreamSources, StreamToken } from "./types";

export type ChatStreamEvent =
  | { event: "meta"; data: StreamMeta }
  | { event: "sources"; data: StreamSources }
  | { event: "token"; data: StreamToken }
  | { event: "done"; data: StreamDone }
  | { event: "error"; data: StreamError };

const KNOWN_EVENTS = new Set(["meta", "sources", "token", "done", "error"]);

/** The response is not an event stream (old browser or a proxy that rewrote it). */
export class StreamUnavailableError extends Error {
  constructor() {
    super("Streaming is not available");
    this.name = "StreamUnavailableError";
  }
}

/** The stream ended without a terminal `done` or `error` event. */
export class StreamInterruptedError extends Error {
  constructor() {
    super("The connection was interrupted before the answer finished.");
    this.name = "StreamInterruptedError";
  }
}

/**
 * POST /chat/stream and yield its server-sent events. HTTP errors raised before the
 * stream starts (404, 422, 429...) throw ApiError; abort the signal to stop.
 */
export async function* streamChat(body: ChatRequest, signal: AbortSignal): AsyncGenerator<ChatStreamEvent, void, void> {
  let res: Response;
  try {
    res = await fetch(apiUrl("/chat/stream"), {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        ...csrfHeaders("POST"),
      },
      body: JSON.stringify(body),
      signal,
      credentials: "same-origin",
    });
  } catch (err) {
    throw isAbortError(err) ? err : networkError();
  }
  if (!res.ok) throw await errorFromResponse(res);
  if (!res.body || !(res.headers.get("content-type") ?? "").includes("text/event-stream")) {
    await res.body?.cancel().catch(() => undefined);
    throw new StreamUnavailableError();
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  let finished = false;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const message of parser.push(decoder.decode(value, { stream: true }))) {
        if (!KNOWN_EVENTS.has(message.event)) continue;
        const event = { event: message.event, data: JSON.parse(message.data) } as ChatStreamEvent;
        if (event.event === "done" || event.event === "error") finished = true;
        yield event;
      }
    }
  } catch (err) {
    if (isAbortError(err)) throw err;
    throw new StreamInterruptedError();
  } finally {
    reader.cancel().catch(() => undefined);
  }
  if (!finished) throw new StreamInterruptedError();
}
