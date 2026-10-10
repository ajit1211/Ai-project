/**
 * An assistant reply as Markdown: GitHub flavour (tables, task lists, strikethrough,
 * autolinks) and fenced code highlighted by its language.
 *
 * A reply is untrusted text, so:
 * - raw HTML in it is shown as text, never rendered (react-markdown's default; there is
 *   deliberately no rehype-raw);
 * - only absolute http, https and mailto URLs survive. Anything else (javascript:, data:,
 *   vbscript:, relative paths) is dropped, which leaves the link inert;
 * - links open in a new tab that cannot reach back into this one;
 * - images are shown as links instead of being loaded, so a reply cannot make the browser
 *   fetch an arbitrary address (a known way to leak conversation text through a URL).
 */

import "highlight.js/styles/github.css";

import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

const SAFE_PROTOCOLS: ReadonlySet<string> = new Set(["http:", "https:", "mailto:"]);

/**
 * react-markdown's `urlTransform`: the URL to keep, normalized by the URL parser exactly as
 * the browser would read it, or null to drop it.
 */
export function safeUrl(url: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(url); // A relative or malformed URL throws.
  } catch {
    return null;
  }
  return SAFE_PROTOCOLS.has(parsed.protocol) ? parsed.href : null;
}

const components: Components = {
  a: ({ href, title, children }) =>
    href ? (
      <a href={href} title={title} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    ) : (
      <span className="markdown-inert-link">{children}</span>
    ),
  img: ({ src, alt }) => {
    const label = `[${alt || "image"}]`;
    return typeof src === "string" && src ? (
      <a href={src} target="_blank" rel="noopener noreferrer">
        {label}
      </a>
    ) : (
      <span>{label}</span>
    );
  },
  // Wide tables scroll inside the reply instead of widening the page.
  table: ({ children }) => (
    <div className="markdown-table">
      <table>{children}</table>
    </div>
  ),
};

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

/** Memoized: while a reply streams, only that reply is parsed again. */
export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={rehypePlugins}
        urlTransform={safeUrl}
        components={components}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
