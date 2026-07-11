import { describe, expect, it } from "vitest";

import {
  isTerminalRunStatus,
  runStatusToneClass,
  scrapeResultTextClass,
} from "./run-quality";

describe("run quality presentation", () => {
  it("treats degraded as a terminal queue result", () => {
    expect(isTerminalRunStatus("degraded")).toBe(true);
    expect(isTerminalRunStatus("running")).toBe(false);
  });

  it("renders degraded with warning rather than success or failure tone", () => {
    expect(runStatusToneClass("degraded")).toContain("text-warning");
    expect(scrapeResultTextClass("degraded")).toBe("text-warning");
    expect(runStatusToneClass("degraded")).not.toContain("text-success");
    expect(runStatusToneClass("degraded")).not.toContain("text-destructive");
  });
});
