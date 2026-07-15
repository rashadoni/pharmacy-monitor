// @vitest-environment jsdom

import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QueryErrorState } from "./query-error-state";

describe("QueryErrorState", () => {
  it("announces the failure and exposes a working retry action", () => {
    const retry = vi.fn();
    render(
      <QueryErrorState
        message="Məlumatlar yüklənmədi"
        retryLabel="Yenidən cəhd et"
        onRetry={retry}
      />,
    );

    expect(screen.getByRole("alert").textContent).toContain("Məlumatlar yüklənmədi");
    fireEvent.click(screen.getByRole("button", { name: "Yenidən cəhd et" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });
});
