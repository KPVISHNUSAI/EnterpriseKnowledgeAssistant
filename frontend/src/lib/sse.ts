/**
 * Incremental Server-Sent Events parser (WHATWG "event stream interpretation").
 * EventSource cannot POST, so the chat client reads a fetch() stream and feeds the
 * decoded text here in whatever chunks the network delivers them.
 */
export interface SseMessage {
  event: string;
  data: string;
  id: string | null;
}

export class SseParser {
  private pending = ""; // incomplete trailing line
  private skipLf = false; // previous chunk ended with CR: swallow the LF that may follow
  private started = false;
  private event = "";
  private data: string[] = [];
  private id: string | null = null;

  /** Feed the next chunk of text; returns the messages completed by it. */
  push(chunk: string): SseMessage[] {
    if (!this.started) {
      this.started = true;
      if (chunk.startsWith("﻿")) chunk = chunk.slice(1);
    }
    if (this.skipLf) {
      this.skipLf = false;
      if (chunk.startsWith("\n")) chunk = chunk.slice(1);
    }
    const text = this.pending + chunk;
    const messages: SseMessage[] = [];
    let start = 0;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (ch !== "\n" && ch !== "\r") continue;
      this.line(text.slice(start, i), messages);
      if (ch === "\r") {
        if (text[i + 1] === "\n") i++;
        else if (i + 1 === text.length) this.skipLf = true;
      }
      start = i + 1;
    }
    this.pending = text.slice(start);
    return messages;
  }

  private line(line: string, out: SseMessage[]): void {
    if (line === "") {
      // A blank line dispatches the event; an event without data is dropped.
      if (this.data.length > 0) {
        out.push({ event: this.event || "message", data: this.data.join("\n"), id: this.id });
      }
      this.event = "";
      this.data = [];
      return;
    }
    if (line.startsWith(":")) return; // comment, e.g. the ": keep-alive" heartbeat
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") this.data.push(value);
    else if (field === "id" && !value.includes("\0")) this.id = value;
    // "retry" and unknown fields are ignored
  }
}
