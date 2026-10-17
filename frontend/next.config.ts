import type { NextConfig } from "next";

// Where /api/* is proxied to. Next.js bakes rewrite destinations into the build output, so the
// Docker image re-applies API_INTERNAL_URL at start-up (see docker-entrypoint.mjs).
const apiUrl = (process.env.API_INTERNAL_URL ?? "http://localhost:8081").replace(/\/+$/, "");
const isDev = process.env.NODE_ENV !== "production";

// Next.js needs inline scripts/styles (no nonce setup); everything else is locked to this origin.
// frame-src allows https: because the PDF preview can redirect to a presigned object-storage URL.
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self'",
  "connect-src 'self'",
  `frame-src 'self' https:${isDev ? " http:" : ""}`,
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'self'",
].join("; ");

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  agentRules: false, // do not let `next dev` write an AGENTS.md into the repository
  experimental: {
    // The /api rewrite proxy buffers request bodies up to this size (default 10 MB) and silently
    // truncates larger ones. Keep it above the API's MAX_UPLOAD_MB (default 50) plus multipart
    // overhead; the API enforces the real limit with a 413.
    proxyClientMaxBodySize: "100mb",
    // Default 30 s (idle). A cold API start that loads models, or a non-streaming /chat on a slow
    // LLM, can take longer; SSE streams stay active anyway (the API sends keep-alives every 15 s).
    proxyTimeout: 300_000,
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
  async headers() {
    return [
      {
        // Proxied /api responses keep the API's own headers (Next.js does not add these to them).
        source: "/((?!api/).*)",
        headers: [
          { key: "Content-Security-Policy", value: contentSecurityPolicy },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "SAMEORIGIN" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
