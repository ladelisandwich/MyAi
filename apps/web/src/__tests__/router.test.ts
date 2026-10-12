import { describe, expect, it } from "vitest";
import { parseHash } from "../lib/router";

describe("parseHash", () => {
  it("defaults to chat", () => expect(parseHash("").page).toBe("chat"));
  it("parses page, params and query", () => {
    const r = parseHash("#/jobs/job_123?x=1");
    expect(r.page).toBe("jobs");
    expect(r.rest).toEqual(["job_123"]);
    expect(r.query.get("x")).toBe("1");
  });
});
