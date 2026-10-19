export interface PdfPage {
  /** Printed at the top of the page and used as the page's bookmark (the outline entry). */
  heading: string;
  lines: string[];
}

/** PDF string literal; the fixtures are plain ASCII. */
const literal = (text: string) => `(${text.replace(/[\\()]/g, "\\$&")})`;

/**
 * A small valid PDF: one Helvetica text page per entry and one bookmark per page. The ingestion
 * worker turns bookmarks into sections, and chunks never cross sections, so every page ends up in
 * its own chunk and a search hit maps to a known page.
 */
export function makePdf(pages: PdfPage[]): Buffer {
  const count = pages.length;
  const pageObject = (i: number) => 5 + i;
  const contentObject = (i: number) => 5 + count + i;
  const bookmarkObject = (i: number) => 5 + 2 * count + i;

  const objects: string[] = [];
  objects[1] = `<< /Type /Catalog /Pages 2 0 R /Outlines 4 0 R /PageMode /UseOutlines >>`;
  objects[2] = `<< /Type /Pages /Kids [${pages.map((_, i) => `${pageObject(i)} 0 R`).join(" ")}] /Count ${count} >>`;
  objects[3] = `<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>`;
  objects[4] = `<< /Type /Outlines /First ${bookmarkObject(0)} 0 R /Last ${bookmarkObject(count - 1)} 0 R /Count ${count} >>`;
  pages.forEach((page, i) => {
    objects[pageObject(i)] =
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents ${contentObject(i)} 0 R >>`;
    const stream = [
      "BT",
      "/F1 18 Tf",
      "72 720 Td",
      `${literal(page.heading)} Tj`,
      "/F1 12 Tf",
      "0 -30 Td",
      "16 TL",
      ...page.lines.flatMap((line, n) => (n === 0 ? [`${literal(line)} Tj`] : ["T*", `${literal(line)} Tj`])),
      "ET",
    ].join("\n");
    objects[contentObject(i)] = `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`;
    const links = [
      i > 0 ? `/Prev ${bookmarkObject(i - 1)} 0 R` : "",
      i < count - 1 ? `/Next ${bookmarkObject(i + 1)} 0 R` : "",
    ].join(" ");
    objects[bookmarkObject(i)] =
      `<< /Title ${literal(page.heading)} /Parent 4 0 R ${links} /Dest [${pageObject(i)} 0 R /Fit] >>`;
  });

  let body = "%PDF-1.4\n";
  const entries = ["0000000000 65535 f \n"];
  for (let n = 1; n < objects.length; n++) {
    entries.push(`${String(body.length).padStart(10, "0")} 00000 n \n`);
    body += `${n} 0 obj\n${objects[n]}\nendobj\n`;
  }
  const xref = `xref\n0 ${objects.length}\n${entries.join("")}`;
  body += `${xref}trailer\n<< /Size ${objects.length} /Root 1 0 R >>\nstartxref\n${body.length}\n%%EOF\n`;
  return Buffer.from(body, "latin1");
}
