import { describe, expect, it } from "vitest";
import { citationHref, citationIdFromHref, citedSourceIds, splitCitations } from "../citations";

describe("splitCitations", () => {
  it("splits text around a marker", () => {
    expect(splitCitations("Revenue grew [S1] last year.")).toEqual([
      { type: "text", text: "Revenue grew " },
      { type: "cite", id: "S1" },
      { type: "text", text: " last year." },
    ]);
  });

  it("expands grouped markers into one citation each", () => {
    expect(splitCitations("See [S1, S2] and [S3;S4].").filter((p) => p.type === "cite")).toEqual([
      { type: "cite", id: "S1" },
      { type: "cite", id: "S2" },
      { type: "cite", id: "S3" },
      { type: "cite", id: "S4" },
    ]);
  });

  it("handles adjacent markers and multi-digit labels", () => {
    expect(splitCitations("[S1][S12]")).toEqual([
      { type: "cite", id: "S1" },
      { type: "cite", id: "S12" },
    ]);
  });

  it("leaves lookalikes alone", () => {
    for (const text of ["[S]", "[s1]", "[S1a]", "[1]", "[S1", "S1]", "[S1234]", "[note S1]"]) {
      expect(splitCitations(text)).toEqual([{ type: "text", text }]);
    }
  });

  it("returns nothing for empty text", () => {
    expect(splitCitations("")).toEqual([]);
  });
});

describe("citedSourceIds", () => {
  it("lists unique ids in order of first appearance", () => {
    expect(citedSourceIds("A [S2]. B [S1, S2]. C [S3].")).toEqual(["S2", "S1", "S3"]);
  });
});

describe("citation links", () => {
  it("round-trips an id through its href", () => {
    expect(citationIdFromHref(citationHref("S7"))).toBe("S7");
  });

  it("does not treat ordinary links as citations", () => {
    for (const href of [undefined, "", "#top", "https://example.com/#cite-S1", "#cite-S1x", "#cite-"]) {
      expect(citationIdFromHref(href)).toBeNull();
    }
  });
});
