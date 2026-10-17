/** Where to go after sign-in. Only same-origin paths are accepted (no open redirects). */
export function safeNext(value: string | null | undefined, fallback = "/dashboard"): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return fallback;
  if (value.includes("\\") || /[\u0000-\u001f]/.test(value)) return fallback;
  if (/^\/(login|register)(\/|\?|#|$)/.test(value)) return fallback;
  try {
    if (new URL(value, "http://placeholder.invalid").origin !== "http://placeholder.invalid") return fallback;
  } catch {
    return fallback;
  }
  return value;
}

export function loginUrl(next: string): string {
  return `/login?next=${encodeURIComponent(next)}`;
}
