import { describe, expect, it } from "vitest";

import {
  isTerminalRunStatus,
  newerProblemAttempt,
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

describe("newer problem attempt of a site", () => {
  const good = { id: 1160, status: "degraded" };

  it("does not call the latest good run a newer problem", () => {
    // Сбор с изолированными карточками бэкенд отдаёт годным: run и попытка — один прогон.
    expect(newerProblemAttempt({ run: good, latest_attempt: good })).toBeNull();
  });

  it("warns about a failed or degraded attempt newer than the good run", () => {
    const failed = { id: 1161, status: "failed" };
    expect(newerProblemAttempt({ run: good, latest_attempt: failed })).toBe(failed);
    const degraded = { id: 1162, status: "degraded" };
    expect(newerProblemAttempt({ run: null, latest_attempt: degraded })).toBe(degraded);
  });

  it("stays quiet about a newer attempt that is still running", () => {
    expect(
      newerProblemAttempt({ run: good, latest_attempt: { id: 1163, status: "running" } }),
    ).toBeNull();
    expect(newerProblemAttempt({ run: null, latest_attempt: null })).toBeNull();
  });
});
