import { ApiError, type LoginLinkMail } from "./api";

/** Переводчик next-intl в пространстве `users`. */
export type LoginLinkMailTranslate = (
  key: string,
  values?: Record<string, string | number>,
) => string;

/**
 * Письмо со ссылкой не ушло или приём не подтверждён: повторная отправка
 * ответила 424, и в теле — исход письма. `null` — ошибка другого рода: 400, 409,
 * сеть, 5xx.
 */
export function loginLinkMailFromError(err: unknown): LoginLinkMail | null {
  if (!(err instanceof ApiError) || err.status !== 424) return null;
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
 * до ящика получателя нам не видно. И при сбое разговора с сервером — «приём
 * не подтверждён», а не «не отправлено»: таймаут после конца письма или отказ
 * на прощании (QUIT) бывают, когда сервер письмо уже взял.
 */
export function loginLinkMailText(t: LoginLinkMailTranslate, mail: LoginLinkMail): string {
  if (mail.sent) return t("mail_accepted");
  if (mail.reason === "smtp_not_configured") {
    return t("mail_not_sent", { reason: t("mail_reason_smtp_not_configured") });
  }
  const code = loginLinkMailCode(mail) || "—";
  const reason =
    mail.reason === "smtp_refused"
      ? t("mail_reason_smtp_refused", { code })
      : t("mail_reason_send_failed", { code });
  return t("mail_not_confirmed", { reason });
}
