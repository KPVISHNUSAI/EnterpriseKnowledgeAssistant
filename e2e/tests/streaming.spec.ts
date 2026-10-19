import { expect, test } from "../support/test";
import { incidentRunbook } from "../support/documents";
import { ask } from "../support/ui";
import { STREAM_DELAY_MS } from "../stack/config";

const QUESTION = "What is the incident escalation process?";

test.beforeEach(async ({ org }) => {
  const document = await org.api.upload({ workspaceId: org.workspaceId, ...incidentRunbook });
  await org.api.waitUntilIndexed(document.id);
});

test("the answer reaches the browser piece by piece through the Next.js proxy", async ({ org }) => {
  const { page } = org;
  await page.goto("/chat");

  await test.step("on the wire: separate token events, spread over time, then done", async () => {
    // Reads POST /chat/stream like the app does and timestamps every event as it arrives.
    const stream = await page.evaluate(async (question) => {
      const csrf = /eka_csrf=([^;]+)/.exec(document.cookie)?.[1] ?? "";
      const started = performance.now();
      const response = await fetch("/api/v1/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream", "X-CSRF-Token": csrf },
        body: JSON.stringify({ message: question }),
      });
      const events: Array<{ event: string; at: number; text?: string }> = [];
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        for (let end = buffer.indexOf("\n\n"); end >= 0; end = buffer.indexOf("\n\n")) {
          const block = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          const event = /^event: (.+)$/m.exec(block)?.[1];
          if (!event) continue; // keep-alive comment
          const data = JSON.parse(/^data: (.+)$/m.exec(block)![1]!);
          events.push({ event, at: performance.now() - started, text: event === "token" ? data.text : undefined });
        }
      }
      return {
        type: response.headers.get("content-type"),
        cacheControl: response.headers.get("cache-control"),
        events,
      };
    }, QUESTION);

    expect(stream.type).toContain("text/event-stream");
    expect(stream.cacheControl).toContain("no-transform"); // what keeps Next.js from buffering the proxied stream
    const names = stream.events.map((e) => e.event);
    expect(names.slice(0, 2)).toEqual(["meta", "sources"]);
    expect(names.at(-1)).toBe("done");
    const tokens = stream.events.filter((e) => e.event === "token");
    expect(tokens.length).toBeGreaterThanOrEqual(5);
    // Each word group is held back by the delay; a buffered stream would deliver them all at once.
    const span = tokens.at(-1)!.at - tokens[0]!.at;
    expect(span).toBeGreaterThan(STREAM_DELAY_MS * (tokens.length - 1) * 0.5);
    expect(stream.events.at(-1)!.at - tokens[0]!.at).toBeGreaterThanOrEqual(span);
    expect(tokens.map((t) => t.text).join("")).toContain("engineering manager");
  });

  await test.step("on screen: the answer grows before it is complete", async () => {
    await page.goto("/chat");
    await page.evaluate(() => {
      const snapshots: Array<{ at: number; text: string }> = [];
      (window as unknown as { answerSnapshots: typeof snapshots }).answerSnapshots = snapshots;
      new MutationObserver(() => {
        const text = document.querySelector('[data-testid="assistant-answer"]')?.textContent ?? "";
        if (text && snapshots.at(-1)?.text !== text) snapshots.push({ at: performance.now(), text });
      }).observe(document.body, { subtree: true, childList: true, characterData: true });
    });

    const answer = await ask(page, QUESTION);
    const finalText = (await answer.getByTestId("assistant-answer").textContent())!;
    const snapshots = await page.evaluate(
      () => (window as unknown as { answerSnapshots: Array<{ at: number; text: string }> }).answerSnapshots,
    );

    expect(snapshots.length).toBeGreaterThanOrEqual(4);
    const lengths = snapshots.map((s) => s.text.length);
    expect(lengths).toEqual([...lengths].sort((a, b) => a - b)); // it only grows
    expect(lengths[0]).toBeLessThan(finalText.length / 2); // the first update is a fraction of the answer
    expect(snapshots.at(-1)!.at - snapshots[0]!.at).toBeGreaterThan(STREAM_DELAY_MS * 3);
    expect(snapshots.at(-1)!.text).toBe(finalText);
    await expect(answer.getByTestId("citation-chip").first()).toHaveAttribute("data-state", "verified");
  });
});

test("Stop cancels the generation and the partial answer is kept", async ({ org }) => {
  const { page } = org;
  await page.goto("/chat");

  // The complete answer, for comparison.
  const complete = await ask(page, QUESTION);
  const fullText = (await complete.getByTestId("assistant-answer").textContent())!;

  await page.getByTestId("new-chat").click();
  await page.getByTestId("chat-input").fill(QUESTION);
  await page.getByTestId("chat-send").click();
  const answer = page.getByTestId("assistant-message");
  await expect(answer).toHaveAttribute("data-status", "streaming");
  await expect(answer.getByTestId("assistant-answer")).toContainText(/\S/); // the first words have arrived
  await expect(page.getByTestId("chat-send")).toHaveCount(0); // replaced by Stop while generating
  await page.getByTestId("chat-stop").click();

  await test.step("the screen shows what arrived and says it stopped", async () => {
    await expect(page.getByTestId("chat-stop")).toHaveCount(0);
    await expect(page.getByTestId("chat-send")).toBeVisible();
    await expect(answer).toHaveAttribute("data-status", "incomplete");
    await expect(answer.getByTestId("answer-banner")).toContainText("Generation stopped");
    const partial = (await answer.getByTestId("assistant-answer").textContent())!;
    expect(partial.length).toBeGreaterThan(0);
    expect(partial.length).toBeLessThan(fullText.length);
  });

  await test.step("the server stopped generating and stored the partial answer", async () => {
    const conversationId = page.url().split("/").pop()!;
    await expect
      .poll(async () => (await org.api.messages(conversationId)).items.find((m) => m.role === "assistant")?.status, {
        timeout: 60_000,
      })
      .toBe("incomplete");
    const stored = (await org.api.messages(conversationId)).items.find((m) => m.role === "assistant")!;
    expect(stored.content.length).toBeGreaterThan(0);
    expect(stored.content.length).toBeLessThan(fullText.length);

    await page.reload();
    await expect(page.getByTestId("assistant-message")).toHaveAttribute("data-status", "incomplete");
    await expect(page.getByTestId("answer-banner")).toContainText("Generation stopped");
  });

  await test.step("the conversation can go on", async () => {
    const next = await ask(page, QUESTION);
    await expect(next.getByTestId("citation-chip").first()).toHaveAttribute("data-state", "verified");
    await expect(page.getByTestId("assistant-message")).toHaveCount(2);
  });
});
