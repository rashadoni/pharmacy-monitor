/**
 * Токен из ссылки-приглашения для формы создания пароля.
 *
 * Ссылку из письма открывает страница API `/auth/link/password`: она читает
 * токен из части адреса после «#», кладёт его в `sessionStorage` вкладки и
 * приводит сюда уже по чистому адресу `/set-password`. Из адреса эта форма токен
 * не берёт намеренно: адрес своей страницы Next.js отправляет серверу в
 * заголовке `Next-Router-State-Tree` — вместе с частью после «#», — а заголовки
 * запроса прокси пишет в журнал.
 *
 * Ключ общий с `LINK_TOKEN_STORAGE_KEY` в `src/login_link_pages.py`; совпадение
 * сверяет `tests/test_login_token_not_in_address.py`.
 */
export const LINK_TOKEN_STORAGE_KEY = "pm_link_token";

export function storedLinkToken(): string | null {
  try {
    const token = window.sessionStorage.getItem(LINK_TOKEN_STORAGE_KEY)?.trim();
    return token ? token : null;
  } catch {
    // Хранилище отключено: страница API об этом уже сказала и сюда не привела.
    return null;
  }
}

/**
 * Токен погашен — пароль задан или сервер отказал по самой ссылке: во вкладке
 * он больше не нужен.
 */
export function forgetLinkToken(): void {
  try {
    window.sessionStorage.removeItem(LINK_TOKEN_STORAGE_KEY);
  } catch {
    // Нечего стирать.
  }
}
