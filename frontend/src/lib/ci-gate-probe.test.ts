import { describe, expect, it } from "vitest";

// Временный файл. Тест падает намеренно: прогон CI на этом коммите показывает,
// что упавший тест красит проверку «Frontend (Next.js)». Следующий коммит
// файл удаляет.
describe("CI gate probe", () => {
  it("fails on purpose", () => {
    expect("vitest step is a gate").toBe("check stayed green");
  });
});
