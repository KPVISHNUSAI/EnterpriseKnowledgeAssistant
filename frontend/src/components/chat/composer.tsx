"use client";

import { useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ArrowUp, Square } from "lucide-react";
import { ScopePicker, type Scope } from "@/components/shared/scope-picker";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

export const MAX_MESSAGE_LENGTH = 2000; // ChatRequest.message max_length

export function Composer({
  scope,
  onScopeChange,
  streaming,
  onSend,
  onStop,
}: {
  scope: Scope;
  onScopeChange: (scope: Scope) => void;
  streaming: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);
  const trimmed = text.trim();

  function resize() {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }

  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!trimmed || streaming) return;
    onSend(trimmed);
    setText("");
    if (area.current) area.current.style.height = "auto";
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <form onSubmit={submit} className="border-t border-slate-200 bg-white px-3 pt-3 pb-2 sm:px-4">
      <div className="mx-auto max-w-3xl">
        <div className="flex items-end gap-2 rounded-xl border border-slate-300 bg-white p-2 focus-within:border-accent-500 focus-within:ring-2 focus-within:ring-accent-200">
          <textarea
            ref={area}
            value={text}
            rows={1}
            maxLength={MAX_MESSAGE_LENGTH}
            placeholder="Ask a question about your documents"
            aria-label="Message"
            data-testid="chat-input"
            className="max-h-50 min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-[15px] outline-hidden placeholder:text-slate-500"
            onChange={(event) => {
              setText(event.target.value);
              resize();
            }}
            onKeyDown={onKeyDown}
          />
          {streaming ? (
            <Button variant="secondary" onClick={onStop} data-testid="chat-stop" aria-label="Stop generating">
              <Square aria-hidden className="size-3.5 fill-current" /> Stop
            </Button>
          ) : (
            <Button
              type="submit"
              variant="primary"
              disabled={!trimmed}
              data-testid="chat-send"
              aria-label="Send message"
              className="size-9 px-0"
            >
              <ArrowUp aria-hidden className="size-4" />
            </Button>
          )}
        </div>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <ScopePicker value={scope} onChange={onScopeChange} disabled={streaming} />
          <p className="text-xs text-slate-500">
            <span className={cn(text.length > MAX_MESSAGE_LENGTH * 0.9 && "font-medium text-amber-700")}>
              {text.length > MAX_MESSAGE_LENGTH * 0.8 ? `${text.length}/${MAX_MESSAGE_LENGTH} · ` : ""}
            </span>
            Answers come from your documents and can contain mistakes. Check the cited sources.
          </p>
        </div>
      </div>
    </form>
  );
}
