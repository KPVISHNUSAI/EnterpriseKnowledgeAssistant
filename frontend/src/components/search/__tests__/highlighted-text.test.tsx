import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HighlightedText } from "../highlighted-text";

describe("HighlightedText", () => {
  it("wraps matching segments in <mark>", () => {
    const { container } = render(
      <p>
        <HighlightedText
          segments={[
            { text: "Employees accrue ", match: false },
            { text: "15 days", match: true },
            { text: " of leave", match: false },
          ]}
          fallback="ignored"
        />
      </p>,
    );
    expect(container.querySelectorAll("mark")).toHaveLength(1);
    expect(container.querySelector("mark")).toHaveTextContent("15 days");
    expect(container).toHaveTextContent("Employees accrue 15 days of leave");
  });

  it("never interprets markup in a document as HTML", () => {
    const { container } = render(
      <p>
        <HighlightedText
          segments={[
            { text: '<img src=x onerror="alert(1)"> ', match: false },
            { text: "<script>alert(2)</script>", match: true },
          ]}
          fallback=""
        />
      </p>,
    );
    expect(container.querySelector("img, script")).toBeNull();
    expect(container.querySelector("mark")?.textContent).toBe("<script>alert(2)</script>");
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
  });

  it("falls back to the plain content when there are no segments", () => {
    const { container } = render(
      <p>
        <HighlightedText segments={[]} fallback="plain passage text" />
      </p>,
    );
    expect(container).toHaveTextContent("plain passage text");
    expect(container.querySelector("mark")).toBeNull();
  });
});
