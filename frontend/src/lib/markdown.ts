import type { Options } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeSanitize, { defaultSchema, type Options as SanitizeSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { remarkCitations } from "./citations";

/**
 * Model output is untrusted. Raw HTML is never enabled in react-markdown, and this schema
 * (the GitHub-flavoured default: no script/style/iframe, no event handlers, only safe URL
 * protocols) is a second layer in case that ever changes. Images are removed outright: a
 * remote <img> in an answer would leak the reader's IP and anything encoded in its URL.
 */
export const sanitizeSchema: SanitizeSchema = {
  ...defaultSchema,
  tagNames: (defaultSchema.tagNames ?? []).filter((tag) => tag !== "img"),
};

export const remarkPlugins: NonNullable<Options["remarkPlugins"]> = [remarkGfm, remarkCitations];

// Sanitize first: highlighting then adds its own token classes to the cleaned tree.
// ignoreMissing: a model-invented language tag must not break rendering of the message.
export const rehypePlugins: NonNullable<Options["rehypePlugins"]> = [
  [rehypeSanitize, sanitizeSchema],
  [rehypeHighlight, { ignoreMissing: true }],
];
