// Container start-up: point the /api proxy at API_INTERNAL_URL, then run the Next.js server.
//
// Next.js evaluates `rewrites()` at build time and writes the result into the build output, so an
// image built once would always proxy to the URL it was built with. This script rewrites that
// destination from the environment, letting one image be promoted across environments with only
// API_INTERNAL_URL changing (whatever value the image was built with is replaced).
import { readFileSync, writeFileSync } from "node:fs";

const MANIFEST = ".next/routes-manifest.json";
// Every file in the standalone output that embeds the rewrite destination.
const FILES = ["server.js", MANIFEST, ".next/required-server-files.json"];
const SOURCE = "/api/:path*";

const raw = process.env.API_INTERNAL_URL ?? "http://localhost:8081";
let apiUrl;
try {
  const url = new URL(raw);
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("not http(s)");
  apiUrl = (url.origin + url.pathname).replace(/\/+$/, "");
} catch {
  console.error(`API_INTERNAL_URL must be an http(s) URL, got "${raw}"`);
  process.exit(1);
}

const rules = Object.values(JSON.parse(readFileSync(MANIFEST, "utf8")).rewrites ?? {}).flat();
const baked = rules.find((rule) => rule.source === SOURCE)?.destination;
if (!baked) {
  console.error(`No ${SOURCE} rewrite in ${MANIFEST}; refusing to start without an API proxy`);
  process.exit(1);
}

const wanted = `${apiUrl}/api/:path*`;
if (baked !== wanted) {
  for (const file of FILES) {
    const text = readFileSync(file, "utf8");
    if (!text.includes(baked)) {
      console.error(`${file} does not contain "${baked}"; unexpected Next.js output`);
      process.exit(1);
    }
    writeFileSync(
      file,
      text.replaceAll(baked, () => wanted),
    );
  }
}
console.log(`API proxy: /api/* -> ${apiUrl}/api/*`);

// The standalone server reads PORT and HOSTNAME from the environment.
await import("./server.js");
