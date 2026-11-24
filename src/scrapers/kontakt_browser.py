"""Kontakt Home через настоящий Chrome на Маке владельца.

Обычный сборщик Kontakt (``kontakt.py``) читает данные страниц сайта
(``/<путь>.data``) HTTP-клиентом, и с сервера сайт отвечает проверкой
Cloudflare. Этот сборщик — тот же самый, с той же логикой обхода и проверкой
полноты, но каждую страницу данных он запрашивает изнутри вкладки
настоящего Google Chrome, открытой на kontakt.az, — тем же ``fetch``, каким
витрина сайта сама подгружает эти данные. Решение владельца 2026-11-24
(разрешено собирать Kontakt настоящим Chrome на его Маке).

Чего здесь нет намеренно (условия того же решения):
  - маскировки: отпечаток браузера, заголовок User-Agent и признак
    автоматизации не подменяются, плагинов «stealth» нет;
  - решения задач Cloudflare: если сайт показывает проверку и настоящий
    Chrome сам её не проходит, сбор останавливается (``CaptchaDetected``) —
    ничего не нажимается и не повторяется;
  - чужих адресов: только kontakt.az, только то, что не запрещает его
    ``robots.txt`` (данные страниц каталога), с паузой между запросами.

Профиль Chrome — отдельный каталог (``profile_dir``), не основной профиль
владельца: Chrome не открывает один профиль в двух процессах.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import structlog

from src.scrapers.base import CaptchaDetected
from src.scrapers.http_catalog import (
    _TRANSIENT_STATUSES,
    PageFetchError,
    looks_like_protection_page,
)
from src.scrapers.kontakt import KontaktScraper

log = structlog.get_logger()

# Сколько ждать, пока открытая главная страница перестанет быть проверкой
# Cloudflare. Настоящий Chrome обычно проходит её сам за секунды; дольше —
# значит, она ждёт человека, а этого сборщик не делает.
_CHALLENGE_WAIT_SEC = 45
# Предел одного запроса: зависший ответ не должен держать сбор вечно.
_REQUEST_TIMEOUT_MS = 60_000
# Запрос изнутри страницы. Только kontakt.az: адрес проверяется до вызова.
_FETCH_JS = """
async ([url, timeoutMs]) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      credentials: "include",
      headers: { Accept: "*/*" },
      signal: controller.signal,
    });
    return { status: response.status, text: await response.text() };
  } catch (error) {
    return { status: null, error: String((error && error.name) || error) };
  } finally {
    clearTimeout(timer);
  }
}
"""


@dataclass
class BrowserResponse:
    """Ответ страницы данных в том виде, в каком его ждёт ``KontaktScraper``."""

    status_code: int
    text: str


class KontaktBrowserScraper(KontaktScraper):
    """``KontaktScraper``, который ходит на сайт через вкладку настоящего Chrome."""

    # Паузы между запросами — как у человека, листающего каталог.
    default_rate_limit_sec = 2.5

    def __init__(self, profile_dir: str | Path, *, headless: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.profile_dir = Path(profile_dir)
        self.browser_headless = headless
        self._page = None
        self._browser_context = None
        self._pw = None

    # -- жизненный цикл ----------------------------------------------------------

    async def __aenter__(self) -> KontaktBrowserScraper:
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        try:
            # channel="chrome" — установленный Google Chrome, а не Chromium
            # Playwright: тот же браузер, которым владелец открывает сайт.
            self._browser_context = await self._pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel="chrome",
                headless=self.browser_headless,
                locale="az-AZ",
            )
            pages = self._browser_context.pages
            self._page = pages[0] if pages else await self._browser_context.new_page()
            await self._open_site()
        except BaseException:
            await self._close_browser()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close_browser()
        await super().__aexit__(exc_type, exc, tb)

    async def _close_browser(self) -> None:
        context, self._browser_context, self._page = self._browser_context, None, None
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        pw, self._pw = self._pw, None
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass

    async def _open_site(self) -> None:
        """Открыть главную kontakt.az и дождаться её самой, а не проверки."""
        await self._page.goto(f"{self.base_url}/", wait_until="domcontentloaded", timeout=60_000)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _CHALLENGE_WAIT_SEC
        while True:
            html = await self._page.content()
            if not looks_like_protection_page(html):
                return
            if loop.time() >= deadline:
                raise CaptchaDetected(
                    "kontakt: Cloudflare keeps showing a check to the real Chrome; "
                    "the collector does not solve it"
                )
            await asyncio.sleep(2)

    # -- запрос страницы ---------------------------------------------------------

    async def _page_fetch(self, url: str) -> dict:
        """Один запрос изнутри вкладки; ``{"status", "text"}`` или ``{"error"}``."""
        # Второй предел — на случай, если зависла сама вкладка, а не запрос.
        return await asyncio.wait_for(
            self._page.evaluate(_FETCH_JS, [url, _REQUEST_TIMEOUT_MS]),
            timeout=_REQUEST_TIMEOUT_MS / 1000 + 15,
        )

    async def fetch(
        self,
        url: str,
        *,
        params: dict | list | None = None,
        headers: dict[str, str] | None = None,
        accept_statuses: tuple[int, ...] = (200,),
    ) -> BrowserResponse:
        """Страница данных сайта или причина, почему её нет.

        Исходы те же, что у HTTP-сборщика: ответ; ``CaptchaDetected`` — сайт
        показал проверку или отказал (403); ``PageFetchError`` — сеть,
        временный или постоянный статус.

        Проверка посреди обхода бывает, когда истёк допуск Cloudflare,
        выданный при открытии главной. Тогда главная открывается заново —
        обычный переход, как у человека, — и запрос повторяется один раз.
        Вторая проверка подряд или проверка на главной — остановка.
        """
        del headers  # заголовки ставит сам Chrome; Accept — в _FETCH_JS
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc.removeprefix("www.") != "kontakt.az":
            raise PageFetchError(f"not a kontakt.az address: {parts.netloc or url[:40]}")
        if params:
            url = f"{url}{'&' if parts.query else '?'}{urlencode(params)}"

        attempts = max(1, self.max_retries)
        last_reason, last_status = "no attempt was made", None
        reopened = False
        attempt = 0
        while attempt < attempts:
            attempt += 1
            if attempt > 1:
                self.fetch_retries += 1
                await asyncio.sleep(min(8.0, 0.5 * 2 ** (attempt - 1)))
            await self._throttle()
            try:
                answer = await self._page_fetch(url)
            except Exception as exc:  # вкладка закрылась, навигация, сбой Chrome
                last_reason, last_status = f"browser error: {type(exc).__name__}", None
                continue
            if not isinstance(answer, dict) or answer.get("status") is None:
                error = answer.get("error") if isinstance(answer, dict) else None
                last_reason, last_status = f"network error: {error or 'no answer'}"[:120], None
                continue
            status = int(answer["status"])
            text = str(answer.get("text") or "")
            if looks_like_protection_page(text) or status == 403:
                if reopened:
                    raise CaptchaDetected(
                        f"kontakt: access refused instead of content (HTTP {status})"
                    )
                reopened = True
                attempt -= 1  # переоткрытие — не попытка из запаса на сбои сети
                log.warning("kontakt_browser_reopening_site", status=status)
                await self._open_site()
                continue
            if status in accept_statuses:
                return BrowserResponse(status_code=status, text=text)
            last_reason, last_status = f"HTTP {status}", status
            if status not in _TRANSIENT_STATUSES:
                break
        raise PageFetchError(last_reason, status=last_status)


def describe(scraper: KontaktBrowserScraper) -> str:
    """Короткая сводка о проходе для журнала сборщика."""
    statuses = scraper._route_statuses
    incomplete = {slug: s.abort_reason for slug, s in statuses.items() if not s.complete}
    return json.dumps(
        {
            "routes": len(statuses),
            "incomplete": len(incomplete),
            "examples": dict(list(incomplete.items())[:5]),
            "retries": scraper.fetch_retries,
        },
        ensure_ascii=False,
    )
