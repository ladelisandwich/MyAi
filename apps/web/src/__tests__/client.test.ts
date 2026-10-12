import { describe, expect, it } from "vitest";
import { readSSE } from "../api/client";

function sseResponse(chunks: string[]): Response {
  const enc = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
}

describe("readSSE", () => {
  it("parses events split across network chunks", async () => {
    const res = sseResponse(['event: meta\ndata: {"a":1}\n\nevent: del', 'ta\ndata: {"text":"Hel"}\n\n: keep-alive\n\n', 'event: delta\ndata: {"text":"lo"}\n\n']);
    const out = [];
    for await (const ev of readSSE(res)) out.push(ev);
    expect(out).toEqual([
      { event: "meta", data: { a: 1 } },
      { event: "delta", data: { text: "Hel" } },
      { event: "delta", data: { text: "lo" } },
    ]);
  });

  it("handles CRLF line endings and non-JSON data", async () => {
    const res = sseResponse(["event: note\r\ndata: plain text\r\n\r\n"]);
    const out = [];
    for await (const ev of readSSE(res)) out.push(ev);
    expect(out).toEqual([{ event: "note", data: "plain text" }]);
  });
});
