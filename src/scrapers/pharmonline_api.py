"""Pharmonline REST-API scraper (2026-08-04) — замена мёртвого DDP-транспорта.

2026-08-03 между 10:00 и 15:00 UTC pharmonline.az переехал с Meteor на Next.js
App Router. Эндпоинт ``wss://pharmonline.az/sockjs/...`` удалён (``/az/sockjs/info``
отдаёт 404), поэтому DDP-клиент виснет на opening handshake и прогон падает с
``TimeoutError: timed out during opening handshake`` и 0 товаров.

Новый фронт ходит в открытый REST-API, который отдаёт **те же Meteor-документы**,
что раньше приходили по DDP::

    GET /api/products?lng=az&page=N&limit=100[&category=<meteor_id>]
      → {"data": [<meteor doc>, ...], "total": N, "page": N, "limit": N, "pages": N}
    GET /api/categories?type=category  → [{"id": <meteor _id>, "path": <slug>, ...}]
    GET /api/products/countries        → [{"id": <meteor _id>, "name": ..., "i18n": {...}}]

Поэтому ``_build_product`` из DDP-модуля переиспользуется БЕЗ изменений, а
``external_id`` остаётся тем же 17-символьным Meteor ``_id`` → история цен и
match-кластеры не рвутся, дублей каталога не возникает.

Отличия от DDP, заложенные в этот модуль
----------------------------------------
* ``limit`` жёстко ≤ 100 (500/1000 → HTTP 400) → полный каталог ≈ 97 страниц.
* Троттлинг NestJS: ``{"statusCode":429,"message":"ThrottlerException: ..."}``.
  429 прилетает и на СВЕЖИХ exit-IP Decodo → лимитер, судя по всему, считает по
  IP Cloudflare-эджа, то есть счётчик общий на весь сайт. Ротация портов от него
  НЕ спасает — лечится только паузой между запросами (``_min_interval``) плюс
  backoff на 429. Окно короткое: замеряно 429 в 06:39:23 → 200 в 06:40:04.
  Пауза по умолчанию намеренно щедрая: полный проход ~10-15 мин против ~1.5 ч
  у DDP, спешить некуда, а выгребать чужой API-бюджет под ноль — плохой сосед.
* Фильтр категории принимает Meteor ``id``, НЕ slug (``category=<slug>`` даёт
  ``total=0``) → в ``__aenter__`` строим обратную карту slug → id.
* ``/api/products/countries`` не отдаёт ``geocode`` (в отличие от DDP-метода
  ``allCountry``) — только названия. ISO-код резолвим через уже существующий
  ``product_policy.normalize_country_code`` (он знает az/ru/en написания).
  Нерезолвенное название просто не попадает в карту → ``_build_product``
  откатывается на legacy customFields, как и раньше.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import time
from typing import Any, AsyncIterator

import httpx
import structlog

from src.scrapers.aptekonline import (
    _decodo_httpx_proxy_for,
    _decodo_page_attempts,
    _decodo_ports,
)
from src.scrapers.base import (
    BaseScraper,
    ScrapedProduct,
    ScrapedPromo,
    SiteScrapeFatalError,
    fatal_proxy_reason,
    site_fatal_error_message,
)
from src.scrapers.pharmonline_ddp import _build_product

log = structlog.get_logger()

API_BASE = "https://pharmonline.az/api"
_PRODUCTS_PATH = f"{API_BASE}/products"
_CATEGORIES_PATH = f"{API_BASE}/categories"
_COUNTRIES_PATH = f"{API_BASE}/products/countries"

# Сервер режет limit > 100 в HTTP 400 (проверено 500 и 1000) — не поднимать.
_MAX_PAGE_SIZE = 100

# Тот же контракт, что в aptekonline: жёсткий блок/исчерпанный баланс прокси →
# ретрай бесполезен, обрываемся; транзиент → пропускаем страницу и идём дальше.
# 429 намеренно НЕ здесь: это троттлинг, он лечится ожиданием (см. _request).
_HARD_BLOCK_STATUSES = {401, 402, 403, 407, 451}
_MAX_CONSECUTIVE_PAGE_FAILURES = 3

# Причины, означающие «каталог уехал под нами», а не поломку: за полный прогон
# (~45 мин) товары успевают появляться и исчезать, из-за чего `total` меняется
# между страницами или уникальных id набирается меньше обещанного. Лечится
# повторным чтением категории, а НЕ ослаблением планки верификации: иначе каждый
# полный прогон уходит в `degraded` и клиент опять получает CRITICAL.
_RACE_ABORT_REASONS = frozenset({"total_changed_during_pagination", "item_count_mismatch"})

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_API_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "az-AZ,az;q=0.9,en;q=0.8",
    "Referer": "https://pharmonline.az/az/products",
}


def _locale() -> str:
    return os.getenv("PHARMONLINE_API_LOCALE", "az").strip().lower() or "az"


def _page_size() -> int:
    raw = os.getenv("PHARMONLINE_API_PAGE_SIZE", str(_MAX_PAGE_SIZE)).strip()
    size = int(raw) if raw.isdigit() and int(raw) > 0 else _MAX_PAGE_SIZE
    return min(size, _MAX_PAGE_SIZE)


def _min_interval() -> float:
    """Базовая пауза между запросами, сек (деф. 7 ≈ 8.5 req/min).

    Это НИЖНЯЯ граница: фактический темп подстраивается вверх при 429 —
    см. `_penalty_step`/`PharmonlineAPIScraper._note_throttled`.
    """
    try:
        return max(0.0, float(os.getenv("PHARMONLINE_API_MIN_INTERVAL", "7")))
    except ValueError:
        return 7.0


def _penalty_step() -> float:
    """На сколько секунд замедлиться после каждого 429 (деф. 3)."""
    try:
        return max(0.0, float(os.getenv("PHARMONLINE_API_PENALTY_STEP", "3")))
    except ValueError:
        return 3.0


def _penalty_max() -> float:
    """Потолок надбавки к паузе, сек (деф. 30 → максимум 37 с на запрос)."""
    try:
        return max(0.0, float(os.getenv("PHARMONLINE_API_PENALTY_MAX", "30")))
    except ValueError:
        return 30.0


def _penalty_decay_after() -> int:
    """Сколько успешных запросов подряд нужно, чтобы отыграть 1 с надбавки (деф. 20).

    Асимметрия намеренная: замедляемся резко (+3 с сразу), ускоряемся медленно.
    Иначе после каждого отката мы бы снова разгонялись в тот же лимит и долбили
    его по кругу — а счётчик у сайта общий, страдали бы и живые покупатели.
    """
    raw = os.getenv("PHARMONLINE_API_PENALTY_DECAY_AFTER", "20").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 20


def _throttle_attempts() -> int:
    raw = os.getenv("PHARMONLINE_API_THROTTLE_ATTEMPTS", "4").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 4


def _throttle_backoff() -> float:
    """Стартовый backoff на HTTP 429, сек (деф. 20; дальше 40, 60, ...)."""
    try:
        return max(1.0, float(os.getenv("PHARMONLINE_API_THROTTLE_BACKOFF", "20")))
    except ValueError:
        return 20.0


def _race_retries() -> int:
    """Сколько раз перечитать категорию, если каталог уехал под нами (деф. 1)."""
    raw = os.getenv("PHARMONLINE_API_RACE_RETRIES", "1").strip()
    return int(raw) if raw.isdigit() else 1


def _max_yield_per_category() -> int:
    raw = os.getenv("PHARMONLINE_API_MAX_YIELD_PER_CATEGORY", "50000").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 50000


def _country_code_map(raw_countries: Any) -> dict[str, str]:
    """Meteor country ``id`` → ISO-3166 alpha-2.

    DDP-метод ``allCountry`` отдавал готовый ``geocode``; REST отдаёт только
    названия, поэтому прогоняем ``name`` и все ``i18n``-варианты через
    ``normalize_country_code`` (там уже лежит az/ru/en словарь). Первое
    совпадение выигрывает. Нераспознанное название в карту не попадает —
    ``_build_product`` тогда откатится на legacy customFields, то есть
    поведение не хуже прежнего, а не «страна выдумана».
    """
    from src.product_policy import normalize_country_code

    mapping: dict[str, str] = {}
    items = raw_countries if isinstance(raw_countries, list) else []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        country_id = entry.get("id") or entry.get("_id")
        if not country_id:
            continue
        names: list[Any] = [entry.get("name")]
        i18n = entry.get("i18n")
        if isinstance(i18n, dict):
            for localized in i18n.values():
                if isinstance(localized, dict):
                    names.append(localized.get("name"))
        for candidate in names:
            if not candidate:
                continue
            code = normalize_country_code(str(candidate))
            if code:
                mapping[str(country_id)] = code
                break
    return mapping


class PharmonlineAPIScraper(BaseScraper):
    """Pharmonline через публичный REST-API нового Next.js-фронта (без браузера).

    Интерфейс тот же, что у ``PharmonlineDDPScraper`` — пайплайн (main.py)
    подменяет один класс другим и больше ничего не знает о транспорте.
    """

    site_name = "pharmonline"
    base_url = "https://pharmonline.az"

    async def __aenter__(self) -> "PharmonlineAPIScraper":  # type: ignore[override]
        # Playwright не инициализируем — API живёт на httpx (как у aptekonline).
        self._locale_code = _locale()
        self._page_size_value = _page_size()
        self._cat_map: dict[str, str] = {}  # meteor id → slug (для _build_product)
        self._slug_to_id: dict[str, str] = {}  # slug → meteor id (для фильтра)
        self._country_map: dict[str, str] = {}
        self._last_request_at: float | None = None
        # Адаптивный темп: надбавка к паузе, накопленная за 429 (см. _note_throttled).
        self._interval_penalty: float = 0.0
        self._ok_streak: int = 0

        ports = _decodo_ports("pharmonline")
        self._port_cycle = itertools.cycle(ports) if ports else None
        self._attempts_per_page = _decodo_page_attempts() if ports else 1
        if ports:
            log.info(
                "pharmonline_api_using_decodo",
                host=os.getenv("DECODO_HOST", "az.decodo.com"),
                ports=len(ports),
                attempts_per_page=self._attempts_per_page,
                min_interval_s=_min_interval(),
            )
        else:
            # Прямой доступ с Hetzner отдаёт 403 (Cloudflare) — предупреждаем явно,
            # иначе прогон молча вернёт 0 товаров.
            log.warning(
                "pharmonline_api_no_proxy",
                note="direct Hetzner IP отдаёт HTTP 403 — нужен DECODO_SITES=...,pharmonline",
            )

        await self._load_category_map()
        await self._load_country_map()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        return None

    # ── HTTP ────────────────────────────────────────────────────────────────

    def _effective_interval(self) -> float:
        """Базовая пауза + накопленная штрафная надбавка за 429."""
        return _min_interval() + self._interval_penalty

    def _note_throttled(self) -> None:
        """Получили 429 → замедлиться и обнулить счётчик успехов."""
        step, cap = _penalty_step(), _penalty_max()
        before = self._interval_penalty
        self._interval_penalty = min(cap, self._interval_penalty + step)
        self._ok_streak = 0
        if self._interval_penalty != before:
            log.info(
                "pharmonline_api_pace_slowed",
                interval_s=round(self._effective_interval(), 1),
                penalty_s=round(self._interval_penalty, 1),
            )

    def _note_ok(self) -> None:
        """Успешный запрос → медленно отыгрываем надбавку назад."""
        if self._interval_penalty <= 0:
            return
        self._ok_streak += 1
        if self._ok_streak >= _penalty_decay_after():
            self._ok_streak = 0
            self._interval_penalty = max(0.0, self._interval_penalty - 1.0)
            log.info(
                "pharmonline_api_pace_relaxed",
                interval_s=round(self._effective_interval(), 1),
                penalty_s=round(self._interval_penalty, 1),
            )

    async def _sleep_for_rate_limit(self) -> None:
        """Выдержать паузу между запросами (общий лимитер сайта — см. модульный docstring)."""
        interval = self._effective_interval()
        if interval <= 0:
            return
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)

    async def _get_once(self, url: str, params: dict | None) -> httpx.Response | None:
        """Один GET. Decodo — ретрай по портам (порт = другой AZ-IP, ~38% флайки 522).

        Паузу темпа выдерживаем перед КАЖДОЙ попыткой, а не раз на вызов: иначе
        серия ретраев по 10 портам ушла бы очередью без задержек и сама влетела бы
        в общий троттлер сайта.

        ``verify`` оставлен дефолтным: Decodo не MITM'ит TLS (проверено вживую),
        в отличие от IPRoyal у aptekonline — там verify=False вынужденный.
        """
        if self._port_cycle is not None:
            last_resp: httpx.Response | None = None
            for _ in range(self._attempts_per_page):
                purl = _decodo_httpx_proxy_for("pharmonline", next(self._port_cycle))
                try:
                    await self._sleep_for_rate_limit()
                    async with httpx.AsyncClient(
                        headers=_API_HEADERS,
                        timeout=httpx.Timeout(60.0),
                        proxy=purl,
                    ) as client:
                        resp = await client.get(url, params=params)
                    self._last_request_at = time.monotonic()
                    if resp.status_code == 200:
                        return resp
                    last_resp = resp
                    if resp.status_code in {402, 407}:
                        # Счёт/баланс прокси — ретрай по другим портам бесполезен
                        # и жжёт остаток. Это единственный по-настоящему фатальный
                        # класс на уровне одного запроса.
                        raise SiteScrapeFatalError(
                            f"Decodo proxy access rejected: HTTP {resp.status_code}"
                        )
                    if resp.status_code == 429:
                        # Троттлер общий на весь сайт — другой exit-IP не поможет,
                        # помогает только пауза. Возвращаем сразу.
                        return resp
                    # 403/401/451 на residential-прокси почти всегда привязаны к
                    # КОНКРЕТНОМУ exit-IP (Cloudflare банит адрес, не аккаунт) —
                    # ради этого и заведена ротация портов. Пробуем следующий IP.
                    # Обрываем сайт только если ВСЕ попытки на РАЗНЫХ IP дали блок
                    # (см. _request) — тогда это уже системный бан, а не плохой адрес.
                    # Регрессия 2026-08-04: раньше здесь стоял ранний return, и
                    # ОДИН 403 на одном адресе ронял весь прогон (remaining=199).
                    if resp.status_code in _HARD_BLOCK_STATUSES:
                        log.warning(
                            "pharmonline_api_ip_blocked",
                            status=resp.status_code,
                            note="меняем exit-IP и пробуем снова",
                        )
                        continue
                except httpx.ProxyError as exc:
                    reason = fatal_proxy_reason(exc)
                    if reason is not None:
                        raise SiteScrapeFatalError(reason) from exc
                    continue
                except httpx.RequestError:
                    continue
            return last_resp
        try:
            await self._sleep_for_rate_limit()
            async with httpx.AsyncClient(
                headers=_API_HEADERS, timeout=httpx.Timeout(30.0)
            ) as client:
                return await client.get(url, params=params)
        except httpx.RequestError:
            return None
        finally:
            self._last_request_at = time.monotonic()

    async def _request(self, url: str, params: dict | None = None) -> Any | None:
        """GET + JSON с уважением к троттлеру.

        Возвращает распарсенный JSON, либо None при транзиентном провале
        (вызывающий решает: пропустить страницу или оборваться). Жёсткий блок и
        отказ прокси поднимают ``SiteScrapeFatalError`` — их глотать нельзя,
        иначе прогон отрапортует ``ok`` с заниженным каталогом.
        """
        backoff = _throttle_backoff()
        for attempt in range(_throttle_attempts()):
            # Пауза темпа — внутри _get_once (перед каждой попыткой по портам).
            resp = await self._get_once(url, params)
            if resp is None:
                return None
            if resp.status_code == 200:
                self._note_ok()
                try:
                    return resp.json()
                except ValueError as exc:
                    log.warning(
                        "pharmonline_api_invalid_json",
                        url=url,
                        error=str(exc),
                    )
                    return None
            if resp.status_code == 429:
                # Троттлер сайта. Окно короткое (~1 мин) — ждём и замедляем темп,
                # чтобы не влететь в тот же лимит на следующем запросе.
                self._note_throttled()
                wait = backoff * (2**attempt)
                log.warning(
                    "pharmonline_api_throttled",
                    url=url,
                    attempt=attempt + 1,
                    max_attempts=_throttle_attempts(),
                    wait_s=wait,
                )
                await asyncio.sleep(wait)
                continue
            if resp.status_code in _HARD_BLOCK_STATUSES:
                raise SiteScrapeFatalError(f"pharmonline API blocked: HTTP {resp.status_code}")
            log.warning(
                "pharmonline_api_bad_status",
                url=url,
                status=resp.status_code,
            )
            return None
        log.error("pharmonline_api_throttle_exhausted", url=url)
        return None

    # ── Справочники ─────────────────────────────────────────────────────────

    async def _load_category_map(self) -> None:
        """Категории: строим id → slug (для _build_product) и slug → id (для фильтра)."""
        try:
            payload = await self._request(_CATEGORIES_PATH, {"type": "category"})
        except SiteScrapeFatalError:
            raise
        except Exception as exc:  # noqa: BLE001 — справочник не должен ронять прогон
            log.warning(
                "pharmonline_api_cat_map_failed",
                error=site_fatal_error_message(exc),
                note="продукты получат raw Mongo _id в поле category",
            )
            return
        items = payload if isinstance(payload, list) else (payload or {}).get("data")
        for entry in items if isinstance(items, list) else []:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("id") or entry.get("_id")
            slug = entry.get("path")
            if cid and slug:
                self._cat_map[str(cid)] = str(slug)
                self._slug_to_id[str(slug)] = str(cid)
        log.info("pharmonline_api_cat_map_loaded", count=len(self._cat_map))

    async def _load_country_map(self) -> None:
        try:
            payload = await self._request(_COUNTRIES_PATH)
        except SiteScrapeFatalError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "pharmonline_api_country_map_failed",
                error=site_fatal_error_message(exc),
            )
            return
        items = payload if isinstance(payload, list) else (payload or {}).get("data")
        total = len(items) if isinstance(items, list) else 0
        self._country_map = _country_code_map(items)
        log.info(
            "pharmonline_api_country_map_loaded",
            resolved=len(self._country_map),
            total=total,
        )

    # ── Скрейп ──────────────────────────────────────────────────────────────

    async def scrape_category(
        self, category_slug: str, limit: int | None = None
    ) -> AsyncIterator[ScrapedProduct]:
        """Отдать товары категории постранично, дедуп по external_id (Meteor _id).

        Телеметрия маршрута (``_set_route_status``) заполняется так же, как в
        DDP-версии — от неё зависит верификация полного каталога. Плюс к прежнему:
        API отдаёт точный ``total`` на категорию, поэтому ``expected_items``
        теперь честный, а не выведенный из «страница короче page_size».
        """
        category_id = self._slug_to_id.get(str(category_slug))
        if category_id is None:
            # Без id фильтровать нечем, а без фильтра мы бы отдали ВЕСЬ каталог
            # под видом одной категории — это молча раздуло бы её и сломало
            # per-category телеметрию. Честно помечаем маршрут неполным.
            log.warning(
                "pharmonline_api_unknown_category",
                category=category_slug,
                known_categories=len(self._slug_to_id),
            )
            self._set_route_status(
                category_slug,
                complete=False,
                abort_reason="unknown_category_slug",
                visited_pages=0,
            )
            return

        max_yield = limit if limit is not None else _max_yield_per_category()
        seen_external_ids: set[str] = set()
        outcome: dict[str, Any] = {}

        for attempt in range(1 + _race_retries()):
            outcome = {}
            async for product in self._scrape_category_pass(
                category_slug,
                category_id,
                seen_external_ids,
                max_yield,
                limit,
                outcome,
            ):
                yield product
            reason = outcome.get("abort_reason")
            if outcome.get("complete") or reason not in _RACE_ABORT_REASONS:
                break
            if attempt + 1 < 1 + _race_retries():
                # Каталог уехал под нами (за полный прогон это ~45 мин — товары
                # успевают появляться и исчезать). Перечитываем категорию целиком:
                # `seen_external_ids` сохраняется, поэтому повторный проход отдаёт
                # ТОЛЬКО недостающее, без дублей. Планку верификации при этом не
                # опускаем — просто честно дочитываем.
                log.info(
                    "pharmonline_api_category_retry",
                    category=category_slug,
                    reason=reason,
                    collected=len(seen_external_ids),
                    expected=outcome.get("expected_items"),
                )

        self._set_route_status(
            category_slug,
            complete=bool(outcome.get("complete")),
            pages_skipped=outcome.get("pages_skipped", 0),
            abort_reason=outcome.get("abort_reason"),
            expected_pages=outcome.get("expected_pages"),
            visited_pages=outcome.get("visited_pages"),
            raw_items=outcome.get("raw_items", 0),
            parsed_items=outcome.get("parsed_items", 0),
            item_failures=outcome.get("item_failures", 0),
            expected_items=outcome.get("expected_items"),
        )

    async def _scrape_category_pass(
        self,
        category_slug: str,
        category_id: str,
        seen_external_ids: set[str],
        max_yield: int,
        limit: int | None,
        outcome: dict[str, Any],
    ) -> AsyncIterator[ScrapedProduct]:
        """Один проход пагинации по категории.

        Отдаёт только товары, которых ещё нет в `seen_external_ids` (поэтому
        повторный проход не дублирует уже выданное), а исход описывает в
        `outcome`. Статус маршрута пишет `scrape_category` — по ПОСЛЕДНЕМУ
        проходу: он перечитывает категорию целиком, поэтому его счётчики
        авторитетны, а суммирование по проходам только задваивало бы их.
        """
        page = 1
        visited_pages = 0
        pages_skipped = 0
        consecutive_failures = 0
        raw_items = 0
        parsed_items = 0
        item_failures = 0
        expected_items: int | None = None
        expected_pages: int | None = None

        # Полнота маршрута = «прошли ли мы ВЕСЬ листинг», то есть строк получено
        # столько же, сколько обещал `total`. Сравнивать с числом УНИКАЛЬНЫХ
        # товаров нельзя: сайт кладёт один и тот же `_id` в категорию по два раза
        # (проверено на `sinir-sistemi-xestelikeri`: total=595, строк 595,
        # уникальных 594 — один товар задвоен самим сайтом). Дедуп — это наша
        # нормализация, а не пробел в покрытии; со старым условием такие категории
        # были обречены висеть в `item_count_mismatch` вечно и держать весь прогон
        # в `degraded`.
        def finish(complete: bool, abort_reason: str | None) -> None:
            outcome.update(
                complete=complete,
                abort_reason=abort_reason,
                expected_items=expected_items,
                expected_pages=expected_pages,
                visited_pages=visited_pages,
                pages_skipped=pages_skipped,
                raw_items=raw_items,
                parsed_items=parsed_items,
                item_failures=item_failures,
            )

        while len(seen_external_ids) < max_yield:
            payload = await self._request(
                _PRODUCTS_PATH,
                {
                    "lng": self._locale_code,
                    "page": page,
                    "limit": self._page_size_value,
                    "category": category_id,
                },
            )
            if payload is None or not isinstance(payload, dict):
                pages_skipped += 1
                consecutive_failures += 1
                log.warning(
                    "pharmonline_api_page_skipped",
                    category=category_slug,
                    page=page,
                    consecutive=consecutive_failures,
                )
                if consecutive_failures >= _MAX_CONSECUTIVE_PAGE_FAILURES:
                    finish(False, "consecutive_page_failures")
                    return
                page += 1
                continue
            consecutive_failures = 0
            visited_pages += 1

            raw_total = payload.get("total")
            if raw_total not in (None, ""):
                try:
                    page_expected = int(raw_total)
                except (TypeError, ValueError):
                    finish(False, "invalid_total")
                    return
                if expected_items is None:
                    expected_items = page_expected
                elif expected_items != page_expected:
                    # Каталог поменялся под нами — этот проход недостоверен.
                    # `scrape_category` перечитает категорию (см. _RACE_ABORT_REASONS).
                    finish(False, "total_changed_during_pagination")
                    return
            raw_pages = payload.get("pages")
            if raw_pages not in (None, ""):
                try:
                    expected_pages = int(raw_pages)
                except (TypeError, ValueError):
                    expected_pages = None

            items = payload.get("data") or []
            if not isinstance(items, list) or not items:
                complete = (
                    item_failures == 0
                    and pages_skipped == 0
                    and (expected_items is None or raw_items == expected_items)
                )
                finish(complete, None if complete else "item_count_mismatch")
                return
            raw_items += len(items)

            for raw in items:
                if not isinstance(raw, dict):
                    item_failures += 1
                    continue
                product = _build_product(raw, self._locale_code, self._cat_map, self._country_map)
                if product is None:
                    item_failures += 1
                    continue
                parsed_items += 1
                if product.external_id in seen_external_ids:
                    continue
                seen_external_ids.add(product.external_id)
                yield product
                if len(seen_external_ids) >= max_yield:
                    finish(
                        False,
                        "requested_limit_reached"
                        if limit is not None
                        else "safety_yield_limit_reached",
                    )
                    return

            # Конец пагинации: сервер сам сказал сколько страниц, либо страница
            # короче запрошенного размера.
            last_page = expected_pages is not None and page >= expected_pages
            if last_page or len(items) < self._page_size_value:
                complete = (
                    item_failures == 0
                    and pages_skipped == 0
                    and (expected_items is None or raw_items == expected_items)
                )
                finish(complete, None if complete else "item_count_mismatch")
                return
            page += 1

        # Вышли по max_yield на входе в цикл (повторный проход, когда всё уже
        # собрано) — исход тот же, что у достигнутого лимита.
        finish(
            False, "requested_limit_reached" if limit is not None else "safety_yield_limit_reached"
        )

    async def scrape_promos(self) -> list[ScrapedPromo]:  # type: ignore[override]
        # Как и в DDP-версии: промо-баннеры рендерятся на главной, отдельная задача.
        return []

    async def fetch_categories(self) -> list[dict[str, Any]]:
        """Справочник категорий для CLI ``category sync-pharmonline``."""
        payload = await self._request(_CATEGORIES_PATH, {"type": "category"})
        items = payload if isinstance(payload, list) else (payload or {}).get("data")
        return [entry for entry in (items or []) if isinstance(entry, dict)]
