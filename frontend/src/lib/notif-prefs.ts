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
 * можно всегда: стоящая галочка ежедневного дайджеста отменяет письма сразу, и
 * запертой она оставляла получателя без писем вовсе.
 */
export function digestToggleLocked(timerOn: boolean | undefined, checked: boolean): boolean {
  return timerOn === false && !checked;
}
