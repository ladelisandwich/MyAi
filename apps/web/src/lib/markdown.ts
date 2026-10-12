import DOMPurify from "dompurify";
import { Marked } from "marked";

// Model output is untrusted: render Markdown, then sanitise with DOMPurify. Raw HTML from
// the model is escaped, links open in a new tab without referrer, and source citations like
// [S1] are highlighted.
const marked = new Marked({ gfm: true, breaks: false });
marked.use({
  renderer: {
    html({ text }) {
      return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    },
  },
});

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer nofollow");
  }
});

export function renderMarkdown(src: string): string {
  const html = marked.parse(src, { async: false }) as string;
  const withCites = html.replace(/\[(S\d{1,2})\]/g, '<span class="cite">[$1]</span>');
  return DOMPurify.sanitize(withCites, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed"],
    FORBID_ATTR: ["style", "onerror", "onload"],
  });
}
