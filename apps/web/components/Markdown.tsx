"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Renders assistant message content as formatted markdown.
 *
 * Safe by default: react-markdown builds React elements (no innerHTML), and we
 * deliberately do NOT enable rehype-raw — model output and ingested external
 * content must never be able to inject HTML. Element styling lives in the
 * `.prose-body` rules in globals.css so it tracks the rest of the chat theme.
 */
export default function Markdown({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ href, children }) => (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        ),
      }}
    >
      {content}
    </ReactMarkdown>
  );
}
