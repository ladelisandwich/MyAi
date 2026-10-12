import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Meter, MockBadge } from "../components/ui";

describe("UI components", () => {
  it("Meter says 'not reported' instead of inventing a value", () => {
    render(<Meter label="VRAM" value={null} max={100} />);
    expect(screen.getByText("not reported")).toBeInTheDocument();
  });
  it("Meter shows a percentage for known values", () => {
    render(<Meter label="CPU" value={42} />);
    expect(screen.getByText("42%")).toBeInTheDocument();
  });
  it("MockBadge clearly labels mock output", () => {
    render(<MockBadge />);
    expect(screen.getByText("MOCK")).toHaveAttribute("title", expect.stringContaining("no AI model"));
  });
});
