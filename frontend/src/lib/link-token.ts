/**
 * Токен из ссылки в письме стоит после «#» (`…/set-password#token=…`).
 *
 * Эту часть адреса браузер серверу не отправляет — ни в запросе страницы, ни в
 * заголовке Referer следующих запросов, — поэтому токена нет в журналах прокси
 * и API. Страница читает его здесь и передаёт телом POST.
 *
 * Из строки запроса («?token=…») токен не читается намеренно: адрес запроса
 * журналы пишут целиком. Формат ссылки задаёт `_send_login_link` в
 * `src/api.py`; совпадение сверяет `tests/test_login_token_not_in_address.py`.
 */
export function linkTokenFromHash(hash: string): string | null {
  const params = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
  const token = params.get("token")?.trim();
  return token ? token : null;
}
