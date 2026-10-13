import { describe, expect, it } from "vitest";

import { passwordProblem, passwordProblemInResponse } from "./password-rule";

describe("passwordProblem", () => {
  it("counts the upper limit in UTF-8 bytes, as the server does", () => {
    expect(passwordProblem("a".repeat(72))).toBeNull();
    expect(passwordProblem("a".repeat(73))).toBe("password_too_long");
    expect(passwordProblem("я".repeat(36))).toBeNull();
    expect(passwordProblem("я".repeat(37))).toBe("password_too_long");
    expect(passwordProblem("ə".repeat(36) + "a")).toBe("password_too_long");
    expect(passwordProblem("😀".repeat(18))).toBeNull();
    expect(passwordProblem("😀".repeat(18) + "a")).toBe("password_too_long");
  });

  it("counts the lower limit in characters, not UTF-16 units", () => {
    expect(passwordProblem("abcde")).toBe("password_too_short");
    expect(passwordProblem("abcdef")).toBeNull();
    // Три эмодзи — шесть единиц UTF-16, но три знака: сервер такой не примет.
    expect(passwordProblem("😀😀😀")).toBe("password_too_short");
  });

  it("names half a surrogate pair before anything else", () => {
    expect(passwordProblem("abcdef\ud800")).toBe("password_not_text");
    expect(passwordProblem("\udc00")).toBe("password_not_text");
    expect(passwordProblem("a".repeat(80) + "\ud800")).toBe("password_not_text");
  });

  it("refuses a NUL: bcrypt reads it as the end of the password", () => {
    expect(passwordProblem("abc\0abc")).toBe("password_not_text");
  });

  it("leaves out the spaces at the edges, as the server does", () => {
    expect(passwordProblem(" abcdef ")).toBeNull();
    expect(passwordProblem(" abcde ")).toBe("password_too_short");
    expect(passwordProblem("      ")).toBe("password_too_short");
    expect(passwordProblem(" ".repeat(10) + "a".repeat(72))).toBeNull();
    expect(passwordProblem("abc def")).toBeNull();
  });
});

describe("passwordProblemInResponse", () => {
  it("reads the reason the server gives", () => {
    const body = {
      detail: [
        { type: "password_too_long", loc: ["body", "new_password"], msg: "…" },
      ],
    };
    expect(passwordProblemInResponse(body)).toBe("password_too_long");
  });

  it("is silent about any other refusal", () => {
    expect(passwordProblemInResponse({ detail: "Пароль уже задан" })).toBeNull();
    expect(
      passwordProblemInResponse({ detail: [{ type: "value_error", msg: "…" }] }),
    ).toBeNull();
    expect(passwordProblemInResponse({})).toBeNull();
    expect(passwordProblemInResponse(null)).toBeNull();
  });
});
