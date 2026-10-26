import { ApiError, type LoginLinkMail } from "./api";

/** Переводчик next-intl в пространстве `users`. */
export type LoginLinkMailTranslate = (
  key: string,
  values?: Record<string, string | number>,
) => string;

/**
 * Письмо со ссылкой не ушло: повторная отправка ответила 503 (почта не
 * настроена) или 502 (сервер отказал или не ответил), и в теле — исход письма.
 * `null` — ошибка другого рода: 4xx, сеть, 502 самого прокси с пустым телом.
 */
export function loginLinkMailFromError(err: unknown): LoginLinkMail | null {
  if (!(err instanceof ApiError) || (err.status !== 502 && err.status !== 503)) {
    return null;
  }
  try {
    const detail = JSON.parse(err.detail)?.detail;
    if (detail?.sent === false && typeof detail.reason === "string") {
      return detail as LoginLinkMail;
    }
  } catch {
    /* не JSON — ответ не от API */
  }
  return null;
}

/** Класс ошибки и коды одной строкой: «SMTPRecipientsRefused 550 5.1.1». */
export function loginLinkMailCode(mail: LoginLinkMail): string {
  return [
    mail.error_type,
    mail.smtp_code,
    mail.smtp_status,
    mail.errno != null ? `errno ${mail.errno}` : null,
  ]
    .filter((part) => part !== null && part !== undefined && part !== "")
    .join(" ");
}

/**
 * Что сказать админу о письме. «Принято почтовым сервером», а не «доставлено»:
 * до ящика получателя нам не видно.
 */
export function loginLinkMailText(t: LoginLinkMailTranslate, mail: LoginLinkMail): string {
  if (mail.sent) return t("mail_accepted");
  const code = loginLinkMailCode(mail) || "—";
  const reason =
    mail.reason === "smtp_not_configured"
      ? t("mail_reason_smtp_not_configured")
      : mail.reason === "smtp_refused"
        ? t("mail_reason_smtp_refused", { code })
        : t("mail_reason_send_failed", { code });
  return t("mail_not_sent", { reason });
}
