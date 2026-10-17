import { describe, expect, it } from "vitest";
import { formatBytes, formatMs, formatRelative, initials, pluralize } from "../format";

describe("format", () => {
  it("formats byte sizes", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(formatBytes(50 * 1024 * 1024)).toBe("50 MB");
  });

  it("formats relative times", () => {
    const now = Date.parse("2026-10-18T12:00:00Z");
    expect(formatRelative("2026-10-18T11:55:00Z", now)).toMatch(/5 minutes ago/);
    expect(formatRelative("2026-10-18T11:59:50Z", now)).toBe("just now");
    expect(formatRelative("2026-10-16T12:00:00Z", now)).toMatch(/2 days ago/);
  });

  it("formats durations, plurals and initials", () => {
    expect(formatMs(340.4)).toBe("340 ms");
    expect(formatMs(1234)).toBe("1.23 s");
    expect(formatMs(null)).toBe("-");
    expect(pluralize(1, "document")).toBe("1 document");
    expect(pluralize(2, "document")).toBe("2 documents");
    expect(initials("Ada Lovelace")).toBe("AL");
    expect(initials("  ")).toBe("?");
  });
});
