import { describe, expect, it } from "vitest";
import { renderMarkdown } from "../lib/markdown";

describe("renderMarkdown", () => {
  it("renders markdown and highlights citations", () => {
    const html = renderMarkdown("**bold** see [S1]\n\n```py\nprint(1)\n```");
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain('<span class="cite">[S1]</span>');
    expect(html).toContain("<pre>");
  });

  it("neutralises raw HTML and scripts from model output", () => {
    const html = renderMarkdown('<img src=x onerror="alert(1)"><script>alert(2)</script> [x](javascript:alert(3))');
    const doc = new DOMParser().parseFromString(`<div>${html}</div>`, "text/html");
    // Raw HTML is shown as literal text, never turned into elements.
    expect(doc.querySelector("img")).toBeNull();
    expect(doc.querySelector("script")).toBeNull();
    expect(doc.querySelector("[onerror]")).toBeNull();
    expect(doc.body.textContent).toContain("<img src=x");
    // javascript: URLs are stripped from links.
    const a = doc.querySelector("a");
    expect(a?.getAttribute("href") ?? "").not.toMatch(/^javascript:/i);
  });

  it("adds safe link attributes", () => {
    const html = renderMarkdown("[home](https://example.org)");
    expect(html).toContain('rel="noopener noreferrer nofollow"');
    expect(html).toContain('target="_blank"');
  });
});
