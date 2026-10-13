import type { NotifPrefs } from "./api";

type Flags = Pick<NotifPrefs, "can_edit" | "has_delivery">;

/**
 * Свои уведомления меняет только тот, кому это разрешил сервер: viewer их
 * видит, но задаёт их администратор. Ответ без признака (API старше страницы)
 * читается как «нельзя» — сервер такую правку всё равно отклонит.
 */
export function canEditNotifPrefs(prefs: Flags | null | undefined): boolean {
  return prefs?.can_edit === true;
}

/**
 * Баннер «алерты никуда не доставляются» — только тому, кто может это
 * исправить, и только когда до него не доходит ничего: ни письмо сразу, ни
 * Telegram, ни дайджест. Доходит ли — решает сервер (`has_delivery`); ответ
 * без признака баннер не показывает.
 */
export function shouldShowNotifBanner(prefs: Flags | null | undefined): boolean {
  return canEditNotifPrefs(prefs) && prefs?.has_delivery === false;
}
