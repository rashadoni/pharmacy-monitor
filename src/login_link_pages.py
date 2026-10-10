"""Страницы, которые открывает ссылка из письма: вход и создание пароля.

Ссылка несёт одноразовый токен после «#»: эту часть адреса браузер серверу не
отправляет — ни в запросе страницы, ни в заголовке Referer. Страница читает
токен сама и дальше передаёт его только телом запроса.

Почему страницу отдаёт API, а не фронтенд. Next.js кладёт адрес открытой
страницы вместе с частью после «#» в заголовок `Next-Router-State-Tree` своих
запросов, а заголовки запроса прокси пишет в журнал. 2026-10-10 на проде токен
единственной открытой ссылки лежал в записи Caddy в четырёх полях: адрес,
Referer, Location редиректа и этот заголовок. Поэтому адрес с токеном не
открывает ни одна страница Next:

- вход — страница шлёт токен в `POST /auth/verify` и уходит в дашборд;
- создание пароля — страница кладёт токен в `sessionStorage` вкладки и уходит
  на форму `/set-password` уже с чистым адресом; форма берёт токен оттуда
  (`frontend/src/lib/link-token.ts`, ключ общий — `LINK_TOKEN_STORAGE_KEY`).

Что браузер на самом деле отправляет серверу, проверяет
`tests/test_login_link_in_a_browser.py` — настоящим Chromium.

Тексты — на двух языках, как и само письмо: язык интерфейса у того, кто ещё не
вошёл, неизвестен.
"""

from __future__ import annotations

# Под этим ключом токен ждёт форму создания пароля. Живёт во вкладке: другая
# вкладка и другой сайт его не видят, с закрытием вкладки он пропадает.
LINK_TOKEN_STORAGE_KEY = "pm_link_token"

_PAGE = """<!doctype html>
<html lang="az">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Pharmacy Monitor</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; font-family: system-ui, sans-serif;
         background: #f8fafc; color: #475569; }
  p { margin: 0 24px; font-size: 14px; line-height: 1.7; text-align: center; }
</style>
</head>
<body>
<p id="state">Keçid yoxlanılır…<br>Проверяем ссылку…</p>
<noscript><p>Bu səhifə üçün JavaScript lazımdır.<br>Этой странице нужен JavaScript.</p></noscript>
<script>
(function () {
  var state = document.getElementById("state");
  var token = new URLSearchParams(window.location.hash.slice(1)).get("token");
  function say(az, ru) {
    state.textContent = "";
    state.appendChild(document.createTextNode(az));
    state.appendChild(document.createElement("br"));
    state.appendChild(document.createTextNode(ru));
  }
__SCRIPT__
})();
</script>
</body>
</html>
"""

# Ответ 401 и 422 — ссылка не годится (нет токена, истекла, уже использована):
# дальше страница входа. Любой другой сбой (сеть, 5xx, перезапуск API) токен не
# трогает: адрес остаётся прежним, и обновление страницы повторяет попытку.
_SIGN_IN = """
  function refused() { window.location.replace("/login?error=link"); }
  function failed() {
    say("Keçidi yoxlamaq alınmadı. Səhifəni yeniləyin.",
        "Не удалось проверить ссылку. Обновите страницу.");
  }
  if (!token) { refused(); return; }
  fetch("/auth/verify", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "Accept": "application/json" },
    body: JSON.stringify({ token: token })
  }).then(function (response) {
    if (response.ok) { window.location.replace("/overview"); }
    else if (response.status === 401 || response.status === 422) { refused(); }
    else { failed(); }
  }, failed);
"""

# Ссылка без токена стирает и прежний: форма не должна молча взять токен от
# письма, открытого в этой вкладке раньше.
_SET_PASSWORD = """
  try {
    if (token) { window.sessionStorage.setItem("__KEY__", token); }
    else { window.sessionStorage.removeItem("__KEY__"); }
  } catch (error) {
    say("Brauzer bu keçidi aça bilmir: səhifə yaddaşı söndürülüb. Keçidi başqa brauzerdə açın.",
        "Браузер не может открыть ссылку: хранилище страницы отключено. Откройте её в другом браузере.");
    return;
  }
  window.location.replace("/set-password");
""".replace("__KEY__", LINK_TOKEN_STORAGE_KEY)

SIGN_IN_PAGE = _PAGE.replace("__SCRIPT__", _SIGN_IN.strip("\n"))
SET_PASSWORD_PAGE = _PAGE.replace("__SCRIPT__", _SET_PASSWORD.strip("\n"))
