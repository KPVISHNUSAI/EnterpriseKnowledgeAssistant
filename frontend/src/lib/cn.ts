import { twMerge } from "tailwind-merge";

/**
 * Join class names, skipping falsy values. Later classes win over conflicting earlier ones
 * (`cn("w-full", "w-32")` is `w-32`), which is what lets components accept a `className` override.
 */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return twMerge(parts);
}
