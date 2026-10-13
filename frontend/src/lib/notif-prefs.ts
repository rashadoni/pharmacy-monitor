import type { NotifPrefs } from "./api";

type Flags = Pick<NotifPrefs, "can_edit" | "has_delivery">;

/**
 * Viewer свои уведомления видит, но не меняет: их задаёт администратор.
 * Только для чтения блок становится по прямому «нет» сервера. API, который
 * признака ещё не отдаёт, правку и не отклоняет — с ним всё как раньше.
 */
export function notifPrefsReadOnly(prefs: Flags | null | undefined): boolean {
  return prefs?.can_edit === false;
}

/**
 * Баннер «алерты никуда не доставляются» — только тому, кто может это
 * исправить, и только когда до него не доходит ничего: ни письмо сразу, ни
 * Telegram, ни дайджест. Оба ответа даёт сервер; без любого из них баннера нет.
 */
export function shouldShowNotifBanner(prefs: Flags | null | undefined): boolean {
  return prefs?.can_edit === true && prefs?.has_delivery === false;
}

/**
 * Галочку дайджеста, чей таймер на сервере не идёт, поставить нельзя, а снять
 * можно всегда: запертая стоящая галочка — настройка, которую не убрать.
 * Дайджест получателя один, недельный; ежедневного нет с 2026-10-13.
 */
export function digestToggleLocked(timerOn: boolean | undefined, checked: boolean): boolean {
  return timerOn === false && !checked;
}

/**
 * Что сказать после кнопки «Прислать себе недельный дайджест» (Quick Actions).
 * Письмо уходит только нажавшему, поэтому исходов два: сервер его отправил или
 * слать было нечего — за неделю нет событий либо недельный дайджест у
 * нажавшего выключен. Ключи — из `quick_actions` в `messages/*.json`.
 */
export function digestButtonFeedback(recipientsSent: number): "digest_sent" | "digest_none" {
  return recipientsSent > 0 ? "digest_sent" : "digest_none";
}
