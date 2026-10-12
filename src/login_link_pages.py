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

- вход — страница показывает кнопку «Войти» и по нажатию шлёт токен в
  `POST /auth/verify`, затем уходит в дашборд. Сама, без нажатия, она токен не
  шлёт: ссылку открывает не только человек — её открывают и проверки ссылок в
  почте, исполняющие сценарии страниц, и такая проверка погасила бы ссылку
  раньше получателя. От чужого сайта, который хочет войти посетителем под
  своей записью, кнопка не защищает: `GET /auth/verify?token=…` входит и без
  нажатия, пока он принимает переход браузера;
- создание пароля — страница кладёт токен в `sessionStorage` вкладки и уходит
  на форму `/set-password` уже с чистым адресом; форма берёт токен оттуда
  (`frontend/src/lib/link-token.ts`, ключ общий — `LINK_TOKEN_STORAGE_KEY`).

Что браузер на самом деле отправляет серверу, проверяет
`tests/test_login_link_in_a_browser.py` — настоящим Chromium.

Тексты — на двух языках, как и само письмо: язык интерфейса у того, кто ещё не
вошёл, неизвестен.
"""

from __future__ import annotations

# Под этим ключом токен ждёт форму создания пароля. Живёт во вкладке: другой
# сайт и вкладка, открытая заново, его не видят, с закрытием вкладки он
# пропадает. Копия вкладки, сделанная самим браузером («дублировать»,
# восстановление сеанса), хранилище получает вместе с ней.
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
  main { margin: 0 24px; text-align: center; }
  p { margin: 0; font-size: 14px; line-height: 1.7; }
  button { margin-top: 20px; padding: 11px 28px; border: 0; border-radius: 8px;
           background: #2563eb; color: #fff; font: inherit; font-size: 15px;
           font-weight: 600; cursor: pointer; }
  button:disabled { opacity: 0.6; cursor: default; }
</style>
</head>
<body>
<main>
<p id="state"></p>
<button id="act" type="button" hidden></button>
<noscript><p>Bu səhifə üçün JavaScript lazımdır.<br>Этой странице нужен JavaScript.</p></noscript>
</main>
<script>
(function () {
  var state = document.getElementById("state");
  var token = new URLSearchParams(window.location.hash.slice(1)).get("token");
  var act = document.getElementById("act");
  function say(az, ru) {
    state.textContent = "";
    state.appendChild(document.createTextNode(az));
    state.appendChild(document.createElement("br"));
    state.appendChild(document.createTextNode(ru));
  }
  // Кнопка со словами на обоих языках; что она делает, решает страница.
  function offer(az, ru, pressed, focused) {
    act.textContent = az + " / " + ru;
    act.onclick = pressed;
    act.disabled = false;
    act.hidden = false;
    if (focused) { act.focus(); }
  }
__SCRIPT__
})();
</script>
</body>
</html>
"""

# Токен уходит только по нажатию кнопки — см. описание модуля.
#
# Ответ 401 и 422 — ссылка не годится (истекла, уже использована, испорчена):
# страница говорит об этом сама и предлагает идти дальше — в дашборд, если вход
# уже состоялся в соседней вкладке, иначе тот уведёт на страницу входа. Любой
# другой сбой (сеть, 5xx, перезапуск API) токен не трогает: адрес остаётся
# прежним, кнопка — тоже, повторное нажатие или обновление страницы повторяют
# попытку.
_SIGN_IN = """
  var stop = null;
  function onward() { window.location.replace("/overview"); }
  // Отказ должны прочитать: «Продолжить» появляется с задержкой и без фокуса,
  // чтобы второе нажатие «Войти» (или второй Enter) не увело со страницы.
  function refused(az, ru) {
    act.hidden = true;
    say(az, ru);
    window.setTimeout(function () { offer("Davam edin", "Продолжить", onward, false); }, 800);
  }
  function failed() {
    say("Keçidi yoxlamaq alınmadı. Yenidən cəhd edin.",
        "Не удалось проверить ссылку. Попробуйте ещё раз.");
    act.disabled = false;
    act.focus();
  }
  // Сервер, который не отвечает, страницу вечно не держит.
  function giveUp() { if (stop) { stop.abort(); } }
  function signIn() {
    act.disabled = true;
    say("Keçid yoxlanılır…", "Проверяем ссылку…");
    stop = window.AbortController ? new AbortController() : null;
    var waiting = window.setTimeout(giveUp, 30000);
    fetch("/auth/verify", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ token: token }),
      signal: stop ? stop.signal : undefined
    }).then(function (response) {
      window.clearTimeout(waiting);
      if (response.ok) { onward(); }
      else if (response.status === 401 || response.status === 422) {
        refused("Keçid etibarsızdır və ya artıq istifadə olunub.",
                "Ссылка недействительна или уже использована.");
      }
      else { failed(); }
    }, function () {
      window.clearTimeout(waiting);
      failed();
    });
  }
  if (!token) {
    refused("Keçid natamamdır. Onu məktubdan yenidən açın.",
            "Ссылка неполная. Откройте её из письма ещё раз.");
    return;
  }
  say("Pharmacy Monitor-a daxil olmaq üçün düyməni basın.",
      "Нажмите кнопку, чтобы войти в Pharmacy Monitor.");
  offer("Daxil ol", "Войти", signIn, true);
"""

# Ссылка без токена стирает и прежний: форма не должна молча взять токен от
# письма, открытого в этой вкладке раньше.
_SET_PASSWORD = """
  say("Keçid açılır…", "Открываем ссылку…");
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
