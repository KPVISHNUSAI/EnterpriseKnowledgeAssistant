import { describe, expect, it } from "vitest";
import { SseParser, type SseMessage } from "../sse";

function parseAll(chunks: string[]): SseMessage[] {
  const parser = new SseParser();
  return chunks.flatMap((chunk) => parser.push(chunk));
}

describe("SseParser", () => {
  it("parses a complete event", () => {
    expect(parseAll(['event: token\ndata: {"text":"Hi"}\n\n'])).toEqual([
      { event: "token", data: '{"text":"Hi"}', id: null },
    ]);
  });

  it("defaults the event name to message", () => {
    expect(parseAll(["data: hello\n\n"])).toEqual([{ event: "message", data: "hello", id: null }]);
  });

  it("returns several events from one chunk", () => {
    const messages = parseAll(["event: a\ndata: 1\n\nevent: b\ndata: 2\n\n"]);
    expect(messages.map((m) => [m.event, m.data])).toEqual([
      ["a", "1"],
      ["b", "2"],
    ]);
  });

  it("handles chunk boundaries anywhere, including inside field names and values", () => {
    const wire = 'event: token\ndata: {"text":"Hello"}\n\nevent: done\ndata: {}\n\n';
    for (let split = 1; split < wire.length; split++) {
      const messages = parseAll([wire.slice(0, split), wire.slice(split)]);
      expect(messages.map((m) => [m.event, m.data])).toEqual([
        ["token", '{"text":"Hello"}'],
        ["done", "{}"],
      ]);
    }
  });

  it("handles one character per chunk", () => {
    const wire = "event: token\ndata: ab\n\n";
    expect(parseAll([...wire])).toEqual([{ event: "token", data: "ab", id: null }]);
  });

  it("does not emit an event before its terminating blank line", () => {
    const parser = new SseParser();
    expect(parser.push("event: token\ndata: partial\n")).toEqual([]);
    expect(parser.push("\n")).toHaveLength(1);
  });

  it("ignores comment lines such as keep-alive heartbeats", () => {
    expect(parseAll([": keep-alive\n\n", "event: token\n: note\ndata: x\n\n", ": keep-alive\n\n"])).toEqual([
      { event: "token", data: "x", id: null },
    ]);
  });

  it("joins multi-line data with newlines", () => {
    expect(parseAll(["data: line one\ndata: line two\ndata:\ndata: line four\n\n"])[0]?.data).toBe(
      "line one\nline two\n\nline four",
    );
  });

  it("strips only one leading space after the colon", () => {
    expect(parseAll(["data:  two spaces\n\n"])[0]?.data).toBe(" two spaces");
    expect(parseAll(["data:nospace\n\n"])[0]?.data).toBe("nospace");
  });

  it("supports CRLF and CR line endings, even when CRLF is split across chunks", () => {
    expect(parseAll(["event: a\r\ndata: 1\r\n\r\n"])[0]).toMatchObject({ event: "a", data: "1" });
    expect(parseAll(["event: a\rdata: 1\r\r"])[0]).toMatchObject({ event: "a", data: "1" });
    expect(parseAll(["event: a\r", "\ndata: 1\r", "\n\r", "\n"])[0]).toMatchObject({ event: "a", data: "1" });
  });

  it("drops events that have no data", () => {
    expect(parseAll(["event: ping\n\n", "id: 7\n\n"])).toEqual([]);
  });

  it("keeps event names from leaking into the next event", () => {
    const messages = parseAll(["event: a\ndata: 1\n\ndata: 2\n\n"]);
    expect(messages.map((m) => m.event)).toEqual(["a", "message"]);
  });

  it("records the id field and ignores retry and unknown fields", () => {
    expect(parseAll(["id: 42\nretry: 1000\nfoo: bar\ndata: x\n\n"])).toEqual([
      { event: "message", data: "x", id: "42" },
    ]);
  });

  it("strips a leading byte order mark", () => {
    expect(parseAll(["﻿data: x\n\n"])[0]?.data).toBe("x");
  });

  it("discards an incomplete trailing event", () => {
    expect(parseAll(["event: token\ndata: cut off"])).toEqual([]);
  });

  it("keeps multi-byte characters intact when decoded from split byte chunks", () => {
    const bytes = new TextEncoder().encode('event: token\ndata: {"text":"héllo 🌍"}\n\n');
    const decoder = new TextDecoder();
    const parser = new SseParser();
    const messages: SseMessage[] = [];
    for (let i = 0; i < bytes.length; i += 3) {
      messages.push(...parser.push(decoder.decode(bytes.slice(i, i + 3), { stream: true })));
    }
    expect(JSON.parse(messages[0]?.data ?? "{}")).toEqual({ text: "héllo 🌍" });
  });
});
