import { describe, expect, it } from "vitest";
import az from "../../messages/az.json";
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";
import { ApiError, type LoginLinkMail } from "./api";
import {
  loginLinkMailCode,
  loginLinkMailFromError,
  loginLinkMailText,
  type LoginLinkMailTranslate,
} from "./login-link-mail";

function tFrom(messages: Record<string, string>): LoginLinkMailTranslate {
  return (key, values = {}) => {
    const template = messages[key];
    if (template === undefined) throw new Error(`no message users.${key}`);
    return Object.entries(values).reduce(
      (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
      template,
    );
  };
}

const LOCALES: Record<string, Record<string, string>> = {
  ru: ru.users as Record<string, string>,
  az: az.users as Record<string, string>,
  en: en.users as Record<string, string>,
};

function errorWith(status: number, detail: unknown): ApiError {
  return new ApiError(status, JSON.stringify({ detail }));
}

const REFUSED: LoginLinkMail = {
  sent: false,
  reason: "smtp_refused",
  error_type: "SMTPRecipientsRefused",
  smtp_code: 550,
  smtp_status: "5.1.1",
  errno: null,
};

describe("loginLinkMailFromError", () => {
  it("reads the mail outcome from the API's 502 and 503", () => {
    expect(loginLinkMailFromError(errorWith(502, REFUSED))).toEqual(REFUSED);
    const notConfigured = { sent: false, reason: "smtp_not_configured" };
    expect(loginLinkMailFromError(errorWith(503, notConfigured))).toEqual(notConfigured);
  });

  it("is not fooled by other errors", () => {
    // 502 самого прокси — тело не от API.
    expect(loginLinkMailFromError(new ApiError(502, "Bad Gateway"))).toBeNull();
    expect(loginLinkMailFromError(errorWith(400, "Пользователь деактивирован"))).toBeNull();
    expect(loginLinkMailFromError(errorWith(503, "Financial comparisons are paused"))).toBeNull();
    expect(loginLinkMailFromError(new ApiError(0, "Сеть недоступна"))).toBeNull();
    expect(loginLinkMailFromError(new Error("boom"))).toBeNull();
  });
});

describe("loginLinkMailCode", () => {
  it("joins the error class and its numbers", () => {
    expect(loginLinkMailCode(REFUSED)).toBe("SMTPRecipientsRefused 550 5.1.1");
    expect(
      loginLinkMailCode({
        sent: false,
        reason: "send_failed",
        error_type: "ConnectionRefusedError",
        errno: 111,
      }),
    ).toBe("ConnectionRefusedError errno 111");
  });
});

describe("loginLinkMailText", () => {
  it("never says delivered: the server accepted the letter, the inbox is not visible", () => {
    const t = tFrom(LOCALES.ru);
    expect(loginLinkMailText(t, { sent: true, reason: null })).toBe(
      "Письмо принято почтовым сервером ✓",
    );
  });

  it("names the cause when the letter did not go", () => {
    const t = tFrom(LOCALES.ru);
    expect(loginLinkMailText(t, { sent: false, reason: "smtp_not_configured" })).toBe(
      "Письмо не отправлено: на сервере не настроена почта (нет SMTP_HOST).",
    );
    expect(loginLinkMailText(t, REFUSED)).toBe(
      "Письмо не отправлено: почтовый сервер отказал — SMTPRecipientsRefused 550 5.1.1.",
    );
    expect(
      loginLinkMailText(t, { sent: false, reason: "send_failed", error_type: "KeyError" }),
    ).toBe("Письмо не отправлено: не удалось передать письмо почтовому серверу — KeyError.");
  });

  it.each(Object.keys(LOCALES))("has every text in %s", (locale) => {
    const t = tFrom(LOCALES[locale]);
    const outcomes: LoginLinkMail[] = [
      { sent: true, reason: null },
      { sent: false, reason: "smtp_not_configured" },
      REFUSED,
      { sent: false, reason: "send_failed", error_type: "TimeoutError" },
    ];
    for (const mail of outcomes) {
      const text = loginLinkMailText(t, mail);
      expect(text).not.toMatch(/\{\w+\}/);
    }
    for (const key of ["mail_accepted_hint", "invite_mail_retry", "login_link_error"]) {
      expect(LOCALES[locale][key]).toBeTruthy();
    }
  });
});
