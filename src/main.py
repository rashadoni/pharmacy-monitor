"""CLI и оркестрация ежедневного прогона.

Команды:
    pharmacy-monitor init-db
    pharmacy-monitor run [--dry-run] [--limit N] [--site S]
    pharmacy-monitor scrape [--site S]
    pharmacy-monitor report [--run-id N]
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from src._time import utcnow
from pathlib import Path

import click
import structlog
import yaml
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session

load_dotenv(override=True)

from src import analyzer, matcher, notifier, reporter, storage, watchlist  # noqa: E402
from src.scrapers.aloe import AloeScraper  # noqa: E402
from src.scrapers.aptekonline import AptekonlineScraper  # noqa: E402
from src.scrapers.base import BaseScraper, ScrapeResult  # noqa: E402
from src.scrapers.pharmonline import PharmonlineScraper  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "categories.yaml"

SCRAPER_CLASSES: dict[str, type[BaseScraper]] = {
    "pharmonline": PharmonlineScraper,
    "aptekonline": AptekonlineScraper,
    "aloe": AloeScraper,
}


def _setup_logging(level: str = "INFO") -> None:
    """Двойной output: цветной для terminal + JSON в файл logs/app.jsonl.

    Файл-логи читаются ELK / Loki / простым `jq`. Ротация — logrotate
    (см. provision_vps.sh — еженедельно, 8 архивов, gzip).
    """
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    # File handler — JSONL
    file_handler = logging.FileHandler(log_dir / "app.jsonl", encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter("%(message)s"))

    # Stream handler — terminal
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    root.setLevel(level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Дашборд / VPS — JSON; для интерактивного terminal — ConsoleRenderer
            (
                structlog.processors.JSONRenderer()
                if os.environ.get("PHARMACY_LOG_JSON") == "1"
                else structlog.dev.ConsoleRenderer(colors=False)
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
    )


log = structlog.get_logger()


def load_config() -> dict:
    """Загрузить настройки лимитов из YAML (категории теперь в БД)."""
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def categories_for_site_from_db(session, site: str) -> list[str]:
    """Slug'и активных категорий для сайта — из БД."""
    return watchlist.categories_for_site(session, site)


def maybe_seed_categories(session) -> None:
    """Если в БД нет ни одной категории — заполнить из YAML (миграция)."""
    existing = session.scalar(select(storage.Category).limit(1))
    if existing is None and CONFIG_PATH.exists():
        n = watchlist.seed_categories_from_yaml(session, CONFIG_PATH)
        if n > 0:
            log.info("categories_seeded_from_yaml", count=n, path=str(CONFIG_PATH))


async def scrape_site(
    site: str, slugs: list[str], limit_per_category: int | None
) -> ScrapeResult:
    cls = SCRAPER_CLASSES[site]
    if not slugs:
        log.warning("no_categories_configured", site=site)
        return ScrapeResult(site=site)
    async with cls() as s:
        return await s.scrape(slugs, limit_per_category=limit_per_category)


async def scrape_all(
    sites_with_slugs: dict[str, list[str]], limit_per_category: int | None
) -> list[ScrapeResult]:
    tasks = [
        scrape_site(site, slugs, limit_per_category)
        for site, slugs in sites_with_slugs.items()
    ]
    return await asyncio.gather(*tasks)


async def scrape_watchlist_for_site(
    site: str, urls: list[str]
) -> ScrapeResult:
    """Watchlist-режим: ходим по конкретным URL'ам товаров на одном сайте."""
    cls = SCRAPER_CLASSES[site]
    if not urls:
        return ScrapeResult(site=site)
    async with cls() as s:
        products = await s.scrape_urls(urls)
        try:
            promos = await s.scrape_promos()
        except Exception as e:
            log.warning("promos_failed", site=site, error=str(e))
            promos = []
        return ScrapeResult(site=site, products=products, promos=promos)


async def scrape_watchlist_all(
    urls_by_site: dict[str, list[str]]
) -> list[ScrapeResult]:
    tasks = [scrape_watchlist_for_site(site, urls) for site, urls in urls_by_site.items()]
    return await asyncio.gather(*tasks)


def collect_watchlist_urls(session) -> dict[str, list[str]]:
    """Собрать pinned URLs из watchlist, сгруппированные по сайту."""
    out: dict[str, list[str]] = {site: [] for site in SCRAPER_CLASSES}
    for tp in watchlist.list_tracked(session, active_only=True):
        for link in tp.links:
            if link.url and link.status == "confirmed" and link.site in out:
                out[link.site].append(link.url)
    return out


def auto_match_watchlist(session) -> int:
    """Привязать Product'ы к Match-кластеру для каждого TrackedProduct.

    Логика: для каждой TrackedProduct → получить или создать Match (canonical_name,
    brand, etc.) → найти Product'ы по URL = TrackedProductLink.url и поставить им
    canonical_id. Это даёт мгновенный cross-site matching без эвристики.

    Возвращает кол-во привязанных Product'ов.
    """
    from sqlalchemy import select
    linked = 0
    for tp in watchlist.list_tracked(session, active_only=True):
        # Найти/создать Match для этого TrackedProduct
        match = session.scalar(
            select(storage.Match).where(
                storage.Match.canonical_name == tp.canonical_name,
                storage.Match.is_manual.is_(True),
            )
        )
        if not match:
            match = storage.Match(
                canonical_name=tp.canonical_name,
                canonical_brand=tp.brand,
                canonical_dosage=tp.dosage,
                canonical_pack_size=tp.pack_size,
                confidence=1.0,
                is_manual=True,  # помечаем как ручной чтобы автоматический matcher не перетёр
            )
            session.add(match)
            session.flush()

        # Привязать Product'ы по URL
        for link in tp.links:
            if not link.url:
                continue
            product = session.scalar(
                select(storage.Product).where(
                    storage.Product.site == link.site,
                    storage.Product.url == link.url,
                )
            )
            if product and product.canonical_id != match.id:
                product.canonical_id = match.id
                linked += 1
    session.commit()
    return linked


def _per_category_breakdown(results: list) -> dict[str, int]:
    """Из списка ScrapeResult вернуть {category_label: count_products}.

    Используется для заполнения `Run.products_per_site_category` — клиент
    видит «по каким category-маршрутам скрейпер ходил, сколько товаров увидел».
    Если у product'а нет `category` (редкий случай), он попадает в `'(uncategorized)'`.
    """
    from collections import Counter

    breakdown: Counter[str] = Counter()
    for result in results:
        for sp in result.products:
            breakdown[sp.category or "(uncategorized)"] += 1
    return dict(breakdown)


def _smoke_test_per_site_coverage(
    session: Session, results: list, run: storage.Run, drop_threshold: float = 0.5
) -> None:
    """Если сайт собрал <drop_threshold × среднее за 5 прошлых ok-runs — write AlertEvent.

    Защита от silent regressions типа run #18 где pharmonline собрал 96 vs обычные 231.
    Не валит run, только записывает warning в alert_events.

    Baseline berëт `Run.products_scraped` по historical ok-runs того же сайта.
    `products_scraped` стабильный счётчик «обработанных ScrapedProduct'ов» — он
    не зависит от того, full persist или diff-only writing snapshots. (Раньше
    считали `COUNT(snapshots) per run, site` через JOIN — после diff-only
    (2026-05-09) historical days с full persist давали ~100K, а текущий
    day = 1-5K → ложные срабатывания site_drop. Текущая версия использует
    products_scraped, который стабилен между режимами.)
    """
    from src.storage import AlertEvent, AlertRule, Run
    from sqlalchemy import select, desc

    for result in results:
        site = result.site
        current = len(result.products) if hasattr(result, "products") else 0
        if current == 0:
            continue
        # Среднее за прошлые 5 ok-runs, где этот сайт участвовал. Берём
        # `products_per_site.get(site)` если есть (точная per-site метрика,
        # добавлена 2026-05-11), иначе fallback на `products_scraped`.
        prev_runs = session.scalars(
            select(Run)
            .where(Run.status == "ok", Run.id < run.id, Run.sites_completed.contains(site))
            .order_by(desc(Run.id))
            .limit(5)
        ).all()
        prev_counts: list[int] = []
        for r in prev_runs:
            per_site = (r.products_per_site or {}).get(site)
            if per_site is not None and per_site > 0:
                prev_counts.append(per_site)
            elif (r.products_scraped or 0) > 0:
                # legacy run без products_per_site — берём total как
                # лучшее приближение (точнее sites_completed fail-safe-фильтр уже)
                prev_counts.append(r.products_scraped)
        if len(prev_counts) < 2:
            continue  # недостаточно истории
        avg = sum(prev_counts) / len(prev_counts)
        if avg <= 0:
            continue
        ratio = current / avg
        if ratio < drop_threshold:
            log.warning(
                "smoke_test_site_drop",
                site=site, current=current, avg=round(avg, 1), ratio=round(ratio, 2),
            )
            # Find or create site_drop rule
            rule = session.scalar(
                select(AlertRule).where(AlertRule.rule_type == "site_drop_smoke").limit(1)
            )
            if not rule:
                rule = AlertRule(
                    name=f"smoke_site_drop",
                    rule_type="site_drop_smoke",
                    params={},
                    channels=[],
                    cooldown_hours=6,
                    is_active=True,
                )
                session.add(rule)
                session.flush()
            # NOTE: AlertEvent НЕ имеет поля `run_id` — раньше тут падало
            # `'run_id' is an invalid keyword argument`. run_id зашит в
            # `dedup_key`, этого достаточно для трассируемости.
            session.add(AlertEvent(
                rule_id=rule.id,
                rule_type="site_drop_smoke",
                dedup_key=f"site_drop_smoke|run={run.id}|site={site}",
                severity="warning",
                title=f"Site {site} собрал на {round((1-ratio)*100)}% меньше обычного",
                detail=(
                    f"В этом прогоне site={site} собрал {current} товаров. "
                    f"Среднее за прошлые {len(prev_counts)} ok-runs: {round(avg)}. "
                    f"Возможно сменилась вёрстка или rate-limit."
                ),
                payload={"site": site, "current": current, "avg": round(avg, 1)},
            ))
        else:
            log.info("smoke_test_ok", site=site, current=current, avg=round(avg, 1))
    session.commit()


_PERSIST_CHUNK = 200
"""Размер чанка для pre-fetch / flush / commit. Для SSH-туннеля к prod Postgres
важно держать INSERT'ы небольшими — на 1000-row INSERT с RETURNING туннель
давал `psycopg.OperationalError: SSL error: unexpected eof while reading`
(см. run_id=43, 2026-05-08). 200 безопасно при ~50KB SQL текста."""


def _snapshot_payload_changed(last: dict | None, sp: ScrapedProduct) -> bool:
    """True если цена/скидка/акция отличаются от последнего snapshot'а.

    None last → всегда True (нет истории, надо записать).
    """
    if last is None:
        return True
    return (
        last["price"] != sp.price
        or last["discount_price"] != sp.discount_price
        or last["discount_percent"] != sp.discount_percent
        or last["is_on_sale"] != sp.is_on_sale
        or last["promo_label"] != sp.promo_label
    )


def persist_results(session: Session, run: storage.Run, results: list[ScrapeResult]) -> int:
    """Сохранить ScrapedProduct/Promo в БД, обновить last_seen_at, добавить snapshots.

    Diff-only persist (2026-05-09): для существующих товаров pre-fetch'им
    последний snapshot одной агрегатной SELECT'ой на чанк, и **INSERT'им
    новый snapshot только если цена/скидка/промо реально изменились**.
    Цены аптек редко меняются — в типичный прогон ~95% товаров без
    изменений, и пишутся в `price_snapshots` только реальные дельты. БД
    остаётся в 30-50× худее, persist через SSH-туннель — секунды, а не
    минуты. `products.last_seen_at` обновляется всегда (это и есть
    «видели сегодня»). Analyzer (`src/analyzer.py`) обновлён под эту
    семантику в той же поставке.

    Bulk-optimized (2026-05-08, второй заход): чанки по 200 продуктов внутри
    каждого ScrapeResult (приходят по одному на сайт, не на категорию!).
    Per-chunk: pre-fetch existing → pre-fetch latest snapshots → ORM
    `add_all + flush` для новых продуктов (нужен RETURNING id для FK) →
    Core `insert` для diff-snapshots → commit.

    Returns: общее число spарсенных продуктов (НЕ записанных snapshot'ов —
    после diff-only snapshots может быть существенно меньше, чем products).
    """
    from sqlalchemy import func
    from sqlalchemy import insert as sa_insert

    from src.brand_catalog import extract_brand
    from src.normalize import extract_dosage, extract_pack_size, normalize_name

    total = 0
    for result in results:
        if not result.products and not result.promos:
            continue

        # === Чанкуем продукты внутри одного ScrapeResult ===
        for chunk_start in range(0, len(result.products), _PERSIST_CHUNK):
            chunk_products = result.products[chunk_start : chunk_start + _PERSIST_CHUNK]
            if not chunk_products:
                continue

            # === Pre-fetch existing для этого чанка ===
            ext_ids_by_site: dict[str, list[str]] = {}
            for sp in chunk_products:
                ext_ids_by_site.setdefault(sp.site, []).append(sp.external_id)

            existing_by_key: dict[tuple[str, str], storage.Product] = {}
            for site, ext_ids in ext_ids_by_site.items():
                unique_ext_ids = list(set(ext_ids))
                rows = session.scalars(
                    select(storage.Product).where(
                        storage.Product.site == site,
                        storage.Product.external_id.in_(unique_ext_ids),
                    )
                ).all()
                for p in rows:
                    existing_by_key[(p.site, p.external_id)] = p

            # === Process: update existing in-place, queue new ===
            new_products: list[storage.Product] = []
            prepared: list[tuple] = []  # (sp, normalized, brand, dosage, pack)

            for sp in chunk_products:
                normalized = normalize_name(sp.name)
                # Скрейпер pharmonline/aptekonline почти не достаёт brand из
                # вёрстки → фолбэк на извлечение из названия по каталогу.
                brand = sp.brand or extract_brand(sp.name)
                # Дозировка/пакет: если скрейпер не вернул, извлечём из имени.
                dosage = sp.dosage or extract_dosage(sp.name)
                pack = sp.pack_size or extract_pack_size(sp.name)
                prepared.append((sp, normalized, brand, dosage, pack))

                existing = existing_by_key.get((sp.site, sp.external_id))
                if existing:
                    existing.name = sp.name
                    existing.name_normalized = normalized
                    existing.brand = brand or existing.brand
                    existing.manufacturer = sp.manufacturer or existing.manufacturer
                    existing.dosage = dosage or existing.dosage
                    existing.pack_size = pack or existing.pack_size
                    existing.image_url = sp.image_url or existing.image_url
                    existing.category = sp.category or existing.category
                    existing.last_seen_at = utcnow()
                else:
                    product = storage.Product(
                        site=sp.site,
                        external_id=sp.external_id,
                        url=sp.url,
                        name=sp.name,
                        name_normalized=normalized,
                        brand=brand,
                        manufacturer=sp.manufacturer,
                        category=sp.category,
                        dosage=dosage,
                        pack_size=pack,
                        image_url=sp.image_url,
                        description=sp.description,
                    )
                    new_products.append(product)
                    existing_by_key[(sp.site, sp.external_id)] = product

            # === Flush новых продуктов (нужен RETURNING id для FK) ===
            if new_products:
                session.add_all(new_products)
                session.flush()

            # === Pre-fetch latest snapshots — для diff-only решения ===
            existing_product_ids = [
                p.id for p in existing_by_key.values() if p.id is not None
            ]
            latest_snapshots: dict[int, dict] = {}
            if existing_product_ids:
                # Subquery: latest captured_at per product_id (одна агрегатная
                # SELECT на чанк). PG умеет это эффективно через
                # btree(product_id) index + sort на маленькой выборке.
                latest_at_subq = (
                    select(
                        storage.PriceSnapshot.product_id,
                        func.max(storage.PriceSnapshot.captured_at).label("max_at"),
                    )
                    .where(storage.PriceSnapshot.product_id.in_(existing_product_ids))
                    .group_by(storage.PriceSnapshot.product_id)
                    .subquery()
                )
                latest_rows = session.scalars(
                    select(storage.PriceSnapshot).join(
                        latest_at_subq,
                        (storage.PriceSnapshot.product_id == latest_at_subq.c.product_id)
                        & (storage.PriceSnapshot.captured_at == latest_at_subq.c.max_at),
                    )
                ).all()
                for snap in latest_rows:
                    # Берём первое попавшееся (если несколько с одинаковым
                    # max_at, что маловероятно, дубль разрулится).
                    if snap.product_id not in latest_snapshots:
                        latest_snapshots[snap.product_id] = {
                            "price": snap.price,
                            "discount_price": snap.discount_price,
                            "discount_percent": snap.discount_percent,
                            "is_on_sale": snap.is_on_sale,
                            "promo_label": snap.promo_label,
                        }

            # === Diff-only snapshots: insert только при изменении цены/скидки/промо ===
            snapshot_rows: list[dict] = []
            now = utcnow()
            for sp, _, _, _, _ in prepared:
                product = existing_by_key[(sp.site, sp.external_id)]
                last_data = latest_snapshots.get(product.id)
                if not _snapshot_payload_changed(last_data, sp):
                    # Цена та же — last_seen_at уже обновлён, snapshot не нужен.
                    total += 1
                    continue
                snapshot_rows.append(
                    {
                        "run_id": run.id,
                        "product_id": product.id,
                        "price": sp.price,
                        "discount_price": sp.discount_price,
                        "discount_percent": sp.discount_percent,
                        "is_on_sale": sp.is_on_sale,
                        "promo_label": sp.promo_label,
                        "captured_at": now,
                    }
                )
                total += 1
            if snapshot_rows:
                session.execute(sa_insert(storage.PriceSnapshot), snapshot_rows)

            # === Commit per chunk — bounds transaction, friendly to SSH tunnel ===
            session.commit()

        # === Promos обычно мало (десятки), один batch ОК ===
        if result.promos:
            for promo in result.promos:
                session.add(
                    storage.Promo(
                        run_id=run.id,
                        site=promo.site,
                        title=promo.title,
                        description=promo.description,
                        image_url=promo.image_url,
                        landing_url=promo.landing_url,
                        valid_until=promo.valid_until,
                        raw_data=promo.raw_data or None,
                    )
                )
            session.commit()

    return total


@click.group()
@click.option("--log-level", default="INFO")
def cli(log_level: str) -> None:
    """Pharmacy Monitor CLI."""
    _setup_logging(log_level)


@cli.command("init-db")
def init_db_cmd() -> None:
    """Создать схему БД."""
    storage.init_db()
    click.echo("DB initialized.")


@cli.command("ai-normalize")
@click.option(
    "--site",
    default=None,
    type=click.Choice(["pharmonline", "aptekonline", "aloe"]),
    help="Ограничить одним сайтом",
)
@click.option(
    "--limit",
    default=None,
    type=int,
    help="Максимум продуктов в этом прогоне (smoke-test)",
)
@click.option(
    "--batch-size",
    default=None,
    type=int,
    help="Сколько продуктов отправлять одним LLM-вызовом (default 50)",
)
@click.option(
    "--budget-usd",
    default=None,
    type=float,
    help="Стоп если стоимость превысит N USD (default $10)",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Пересчитать даже cached продукты (после изменения prompt'а)",
)
def ai_normalize_cmd(
    site: str | None,
    limit: int | None,
    batch_size: int | None,
    budget_usd: float | None,
    force: bool,
) -> None:
    """AI-нормализация фарма-атрибутов: active_ingredient, dosage_mg, pack_count.

    Заполняет колонку `Product.normalized_attrs` через LLM (Anthropic Haiku
    по умолчанию). Hash-кэш гарантирует, что неизменённые SKU не зовутся
    повторно. Запускается автоматически в `run` перед matcher (можно отключить
    PHARMACY_AI_NORMALIZE=0); standalone полезен для backfill и retry.

    Примеры:
        pharmacy-monitor ai-normalize                          # все продукты
        pharmacy-monitor ai-normalize --site aloe              # только aloe
        pharmacy-monitor ai-normalize --limit 100              # smoke-test
        pharmacy-monitor ai-normalize --force                  # пересчёт всех
    """
    from src import ai_normalize

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        stats = ai_normalize.normalize_run(
            s,
            site=site,
            limit=limit,
            batch_size=batch_size,
            budget_usd=budget_usd,
            force=force,
        )

    click.echo(
        f"total={stats.products_total}  "
        f"called={stats.products_called}  "
        f"cached={stats.products_cached}  "
        f"failed={stats.products_failed}  "
        f"cost_usd={stats.cost_usd:.4f}"
    )
    if stats.budget_exceeded:
        click.echo("⚠️  Бюджет исчерпан — следующий прогон продолжит pending продукты.")
    if stats.failures:
        click.echo(f"⚠️  {len(stats.failures)} failures (first 3):")
        for f in stats.failures[:3]:
            click.echo(f"   - {f}")


@cli.command("db-check")
@click.option("--fix", is_flag=True, help="Автоматически чинить orphans (удалять)")
def db_check_cmd(fix: bool) -> None:
    """Проверить целостность БД: orphan matches, NULL prices, висящие references.

    Запуск:
        pharmacy-monitor db-check          # только репорт
        pharmacy-monitor db-check --fix    # удалить orphan'ов

    Exit-code: 0 = OK, 1 = найдены проблемы (без --fix), 2 = ошибка SQL
    """
    from sqlalchemy import select as _s, func as _f, delete as _del, text

    storage.init_db()
    Session = storage.make_session()
    issues_found = 0

    with Session() as s:
        # 1. SQLite integrity_check
        if storage.make_engine().url.drivername.startswith("sqlite"):
            result = s.execute(text("PRAGMA integrity_check")).scalar()
            if result != "ok":
                click.echo(f"❌ SQLite integrity_check: {result}")
                sys.exit(2)
            click.echo("✓ SQLite integrity_check: ok")

        # 2. Orphan matches (без products)
        orphan_matches = s.scalars(
            _s(storage.Match).where(~storage.Match.products.any())
        ).all()
        if orphan_matches:
            issues_found += len(orphan_matches)
            click.echo(f"⚠️  Orphan matches (без products): {len(orphan_matches)}")
            if fix:
                for m in orphan_matches:
                    s.delete(m)
                s.commit()
                click.echo(f"   → Удалено {len(orphan_matches)}")

        # 3. Snapshots с product_id ссылающимися на удалённый Product
        stale_snaps = s.scalar(text("""
            SELECT COUNT(*) FROM price_snapshots ps
            LEFT JOIN products p ON p.id = ps.product_id
            WHERE p.id IS NULL
        """))
        if stale_snaps:
            issues_found += stale_snaps
            click.echo(f"⚠️  Snapshots с битой product_id: {stale_snaps}")
            if fix:
                s.execute(text("""
                    DELETE FROM price_snapshots
                    WHERE product_id NOT IN (SELECT id FROM products)
                """))
                s.commit()
                click.echo(f"   → Удалено {stale_snaps}")

        # 4. Products с canonical_id указывающим на удалённый Match
        stale_canon = s.scalar(text("""
            SELECT COUNT(*) FROM products p
            LEFT JOIN matches m ON m.id = p.canonical_id
            WHERE p.canonical_id IS NOT NULL AND m.id IS NULL
        """))
        if stale_canon:
            issues_found += stale_canon
            click.echo(f"⚠️  Products с битой canonical_id: {stale_canon}")
            if fix:
                s.execute(text("""
                    UPDATE products SET canonical_id = NULL
                    WHERE canonical_id NOT IN (SELECT id FROM matches)
                """))
                s.commit()
                click.echo(f"   → Обнулено canonical_id в {stale_canon}")

        # 5. NULL prices (более 80% snapshots без price — подозрительно)
        total_snaps = s.scalar(_s(_f.count(storage.PriceSnapshot.id))) or 0
        null_prices = s.scalar(
            _s(_f.count(storage.PriceSnapshot.id))
            .where(storage.PriceSnapshot.price.is_(None))
        ) or 0
        if total_snaps > 0:
            null_pct = null_prices / total_snaps * 100
            if null_pct > 80:
                issues_found += null_prices
                click.echo(
                    f"⚠️  NULL prices: {null_prices}/{total_snaps} ({null_pct:.0f}%)"
                )
            else:
                click.echo(
                    f"✓ NULL prices в норме: {null_prices}/{total_snaps} ({null_pct:.1f}%)"
                )

        # 6. Дубликаты Products (same site + same external_id) — должно быть 0 за счёт UniqueConstraint
        dup_products = s.scalar(text("""
            SELECT COUNT(*) FROM (
                SELECT site, external_id, COUNT(*) as cnt
                FROM products GROUP BY site, external_id HAVING cnt > 1
            )
        """))
        if dup_products:
            click.echo(f"❌ Duplicate products (site, external_id): {dup_products}")
            issues_found += dup_products

        # 7. Counts summary
        click.echo("")
        click.echo("📊 Summary:")
        for table in ["runs", "products", "matches", "price_snapshots", "alert_events"]:
            n = s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            click.echo(f"   {table:20s} {n}")

    if issues_found and not fix:
        click.echo(f"\n⚠️  Найдено {issues_found} проблем. Используй --fix чтобы исправить.")
        sys.exit(1)
    elif issues_found and fix:
        click.echo(f"\n✓ Исправлено {issues_found} проблем.")
    else:
        click.echo("\n✓ DB целостность: OK")


@cli.command("health-check")
@click.option(
    "--max-age-hours", type=int, default=26,
    help="Алерт если последний прогон старше N часов (по умолчанию 26 — суточный cron + jitter)",
)
@click.option(
    "--min-products", type=int, default=1,
    help="Алерт если последний прогон собрал меньше N товаров",
)
@click.option(
    "--alert-email", is_flag=True,
    help="Отправить email-алерт получателям при warning/critical",
)
@click.option(
    "--quiet-on-ok", is_flag=True,
    help="Без вывода если статус ok (для cron — пишет только при проблемах)",
)
def health_check_cmd(
    max_age_hours: int, min_products: int, alert_email: bool, quiet_on_ok: bool
) -> None:
    """Проверить здоровье системы: stale/failed/empty/site-drop. Exit-code 0=ok, 1=warning, 2=critical."""
    from src.health import check_health, render_alert_html

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        report = check_health(
            s, max_age_hours=max_age_hours, min_products=min_products
        )

    if report.status == "ok" and quiet_on_ok:
        return

    sev_emoji = {"ok": "✓", "warning": "⚠️", "critical": "🔴"}
    click.echo(
        f"{sev_emoji[report.status]} {report.status.upper()} — "
        f"last run #{report.last_run_id} ({report.last_run_status}) "
        f"at {report.last_run_at}"
    )
    for i in report.issues:
        click.echo(f"  [{i.severity}] {i.code}: {i.message}")

    if alert_email and report.status != "ok":
        try:
            html = render_alert_html(report)
            notifier.send_email(
                subject=f"Pharmacy Monitor — {report.status.upper()}",
                html_body=html,
            )
            click.echo("→ Email-алерт отправлен")
        except Exception as e:
            click.echo(f"⚠️ Не удалось отправить email-алерт: {e}", err=True)

    # Exit code для cron-логики
    sys.exit({"ok": 0, "warning": 1, "critical": 2}[report.status])


# ============================================================================
# API — REST для интеграции с ERP клиента
# ============================================================================


@cli.command("api")
@click.option("--port", type=int, default=8080)
@click.option("--host", default="127.0.0.1")
def api_cmd(port: int, host: str) -> None:
    """Запустить REST API (FastAPI + uvicorn) для интеграции с ERP.

    Endpoints:
      GET  /health
      GET  /api/v1/alerts/recent
      GET  /api/v1/products
      GET  /api/v1/comparisons
      GET  /api/v1/margin
      POST /api/v1/inventory/stock      (bulk push from ERP)
      POST /api/v1/inventory/prices     (bulk push from ERP)

    Auth: установить PHARMACY_API_KEY в .env, передавать как X-API-Key header.
    """
    import uvicorn

    storage.init_db()
    if not os.environ.get("PHARMACY_API_KEY"):
        click.echo(
            "⚠️  PHARMACY_API_KEY не задан в .env — API будет отказывать всем запросам "
            "с 503. Добавь любой случайный токен."
        )
    click.echo(f"🚀 API запущен на http://{host}:{port}")
    click.echo(f"   Docs (OpenAPI): http://{host}:{port}/docs")
    uvicorn.run("src.api:app", host=host, port=port, log_level="info")


# ============================================================================
# INVENTORY — остатки + закупочные цены + маржа
# ============================================================================


@cli.group("inventory")
def inventory_group() -> None:
    """Остатки на складе и закупочные цены — agency-mode."""


@inventory_group.command("import-stock")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--source", default="manual_csv", help="Метка источника (для мульти-импорта)")
def inv_import_stock(csv_path: str, source: str) -> None:
    """Импорт CSV с остатками: sku,qty,name."""
    from pathlib import Path

    from src.inventory import import_stock_from_csv

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        result = import_stock_from_csv(s, Path(csv_path), source=source)
    click.echo(
        f"OK: total={result.total} matched={result.matched_to_product} "
        f"unmatched={result.unmatched}"
    )


@inventory_group.command("import-prices")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--source", default="manual_csv")
def inv_import_prices(csv_path: str, source: str) -> None:
    """Импорт закупочных цен: sku,supplier_name,purchase_price,currency,name."""
    from pathlib import Path

    from src.inventory import import_supplier_prices_from_csv

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        result = import_supplier_prices_from_csv(s, Path(csv_path), source=source)
    click.echo(
        f"OK: total={result.total} matched={result.matched_to_product} "
        f"unmatched={result.unmatched}"
    )


@inventory_group.command("margin-report")
@click.option("--limit", type=int, default=20)
def inv_margin_report(limit: int) -> None:
    """Топ-товаров по марже (sale - purchase)."""
    from src.inventory import margin_report

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        rows = margin_report(s)
        if not rows:
            click.echo("(нет данных — нужны и snapshots, и закупочные цены)")
            return
        for r in rows[:limit]:
            stock_emoji = "📦" if r.in_stock else "⏸️"
            click.echo(
                f"  {stock_emoji} {r.name[:50]:50s} "
                f"sale={r.sale_price:>7.2f}  buy={r.purchase_price:>7.2f}  "
                f"margin={r.margin_azn:>+6.2f} ({r.margin_pct:>+5.1f}%)"
            )


# ============================================================================
# TENANTS — multi-tenant скелет (Q2 SaaS-trek)
# ============================================================================


@cli.group("tenant")
def tenant_group() -> None:
    """Multi-tenant: тенанты, пользователи, magic-link auth (скелет Q2)."""


@tenant_group.command("init-default")
def tenant_init_default() -> None:
    """Создать default-тенант (pharmonline) если его нет.

    Backward-compat: все существующие данные (без tenant_id) принадлежат default.
    Запускается автоматически при `init-db`, но можно дёрнуть руками.
    """
    from src import tenants as t_mod
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        t = t_mod.get_or_create_default(s)
        click.echo(f"OK: tenant #{t.id} {t.slug} ({t.name})")


@tenant_group.command("add")
@click.argument("slug")
@click.argument("name")
@click.option("--client-site", default=None)
@click.option("--plan", default="trial", type=click.Choice(["trial", "basic", "pro"]))
def tenant_add(slug: str, name: str, client_site: str | None, plan: str) -> None:
    """Создать нового тенанта."""
    from src import tenants as t_mod
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        try:
            t = t_mod.create_tenant(s, slug, name, client_site=client_site, plan=plan)
            click.echo(f"OK: #{t.id} {t.slug} ({t.name}) plan={t.plan}")
        except ValueError as e:
            raise click.ClickException(str(e))


@tenant_group.command("list")
def tenant_list() -> None:
    """Список тенантов."""
    from src import tenants as t_mod
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        rows = t_mod.list_tenants(s, active_only=False)
        if not rows:
            click.echo("(нет — запусти `tenant init-default`)")
            return
        for t in rows:
            mark = "✓" if t.is_active else "✗"
            click.echo(
                f"  [{mark}] #{t.id:3d} {t.slug:25s} {t.name:30s} "
                f"plan={t.plan:8s} site={t.client_site or '—'}"
            )


@tenant_group.command("add-user")
@click.argument("tenant_slug")
@click.argument("email")
@click.option("--name", default=None)
@click.option("--role", default="admin", type=click.Choice(["admin", "viewer"]))
def tenant_add_user(tenant_slug: str, email: str, name: str | None, role: str) -> None:
    """Добавить пользователя в тенант."""
    from src import tenants as t_mod
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        t = t_mod.get_tenant(s, tenant_slug)
        if not t:
            raise click.ClickException(f"Tenant not found: {tenant_slug}")
        u = t_mod.add_user(s, t.id, email, name=name, role=role)
        click.echo(f"OK: user #{u.id} {u.email} role={u.role}")


@tenant_group.command("issue-token")
@click.argument("email")
@click.option("--ttl-min", type=int, default=30)
def tenant_issue_token(email: str, ttl_min: int) -> None:
    """Выпустить magic-link токен (для тестов SaaS-auth)."""
    from src import tenants as t_mod
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        token = t_mod.issue_magic_token(s, email, ttl_minutes=ttl_min)
        if token:
            click.echo(f"Token: {token}")
            click.echo(f"(в продакшене — отправить ссылкой в email)")
        else:
            click.echo(f"Юзер {email} не найден или неактивен")


# ============================================================================
# ALERTS — правила + dispatch
# ============================================================================


@cli.group("alert")
def alert_group() -> None:
    """Управление алертами."""


_RULE_TYPES = (
    "undercut_threshold",
    "price_drop_pct",
    "new_product",
    "promo_started",
    "price_raise_opportunity",
)


@alert_group.command("add-rule")
@click.argument("rule_type", type=click.Choice(_RULE_TYPES))
@click.option("--name", default=None, help="Имя правила (по умолчанию = тип)")
@click.option("--min-pct", type=float, default=None, help="Порог % для threshold-правил")
@click.option(
    "--site", type=click.Choice(["pharmonline", "aptekonline", "aloe"]),
    default=None, help="Ограничить правило одним сайтом",
)
@click.option(
    "--channels", default="email", help="Список каналов через запятую: email,telegram",
)
@click.option("--cooldown-hours", type=int, default=12)
def alert_add_rule(
    rule_type: str, name: str | None, min_pct: float | None,
    site: str | None, channels: str, cooldown_hours: int,
) -> None:
    """Создать новое правило алерта."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        params: dict = {}
        if min_pct is not None:
            params["min_pct"] = min_pct
        if site:
            params["site"] = site
        rule = storage.AlertRule(
            name=name or rule_type,
            rule_type=rule_type,
            params=params,
            channels=[c.strip() for c in channels.split(",") if c.strip()],
            cooldown_hours=cooldown_hours,
            is_active=True,
        )
        s.add(rule)
        s.commit()
        click.echo(
            f"OK: rule #{rule.id} {rule.rule_type} params={rule.params} "
            f"channels={rule.channels} cooldown={cooldown_hours}h"
        )


@alert_group.command("list-rules")
@click.option("--all", "show_all", is_flag=True, help="Включая выключенные")
def alert_list_rules(show_all: bool) -> None:
    """Список настроенных правил."""
    storage.init_db()
    Session = storage.make_session()
    from sqlalchemy import select
    with Session() as s:
        stmt = select(storage.AlertRule).order_by(storage.AlertRule.id)
        if not show_all:
            stmt = stmt.where(storage.AlertRule.is_active.is_(True))
        rows = s.scalars(stmt).all()
        if not rows:
            click.echo("(нет правил — добавь через `alert add-rule TYPE`)")
            return
        for r in rows:
            mark = "✓" if r.is_active else "✗"
            click.echo(
                f"  [{mark}] #{r.id:3d} {r.rule_type:28s} {r.name:30s} "
                f"params={r.params} channels={r.channels} cd={r.cooldown_hours}h"
            )


@alert_group.command("remove-rule")
@click.argument("rule_id", type=int)
def alert_remove_rule(rule_id: int) -> None:
    """Удалить правило."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        r = s.get(storage.AlertRule, rule_id)
        if not r:
            click.echo(f"Not found: #{rule_id}")
            return
        s.delete(r)
        s.commit()
        click.echo(f"OK: removed #{rule_id}")


@alert_group.command("evaluate")
@click.option(
    "--dispatch", is_flag=True,
    help="Не только посчитать, но и отправить по каналам (email/telegram)",
)
@click.option(
    "--rule-id", type=int, multiple=True,
    help="Прогнать только указанные правила (можно несколько)",
)
def alert_evaluate(dispatch: bool, rule_id: tuple[int, ...]) -> None:
    """Прогнать движок алертов вручную."""
    from src import alerts as alerts_mod

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        fired = alerts_mod.evaluate_rules(
            s, rule_ids=list(rule_id) if rule_id else None
        )
        click.echo(f"Сработало: {len(fired)}")
        for ev in fired:
            click.echo(f"  [{ev.severity}] {ev.rule_type}: {ev.title[:80]}")
            if dispatch:
                results = alerts_mod.dispatch_event(s, ev)
                click.echo(f"     → {results}")


@alert_group.command("recent")
@click.option("--limit", type=int, default=20)
def alert_recent(limit: int) -> None:
    """Последние сработавшие events."""
    from sqlalchemy import desc, select as sa_select

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        events = s.scalars(
            sa_select(storage.AlertEvent)
            .order_by(desc(storage.AlertEvent.created_at))
            .limit(limit)
        ).all()
        if not events:
            click.echo("(нет событий)")
            return
        for ev in events:
            click.echo(
                f"  {ev.created_at.strftime('%Y-%m-%d %H:%M')}  "
                f"[{ev.severity:8s}] {ev.rule_type:28s} {ev.title[:60]}"
            )


# ============================================================================
# TELEGRAM — registration helper + test send
# ============================================================================


@cli.group("telegram")
def telegram_group() -> None:
    """Telegram бот: регистрация, тест-отправка."""


@telegram_group.command("poll")
@click.option(
    "--once", is_flag=True,
    help="Один раз получить getUpdates и выйти (для register-flow)",
)
def telegram_poll(once: bool) -> None:
    """Poll Telegram bot — показать последние сообщения с chat_id'ами.

    Использование: попроси клиента написать боту любое сообщение, потом запусти
    эту команду чтобы увидеть его chat_id и привязать через `recipient update`.
    """
    from src import notifier
    updates = notifier.telegram_get_updates()
    if not updates:
        click.echo("Нет новых сообщений. Попроси клиента написать боту /start.")
        return
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        from_ = msg.get("from") or {}
        text = msg.get("text") or "<no text>"
        click.echo(
            f"  chat_id={chat.get('id')}  "
            f"from={from_.get('username') or from_.get('first_name')}  "
            f"text={text[:60]!r}"
        )


@telegram_group.command("send-test")
@click.argument("chat_id")
@click.option("--text", default="🧪 Тест-сообщение от Pharmacy Monitor")
def telegram_send_test(chat_id: str, text: str) -> None:
    """Отправить тестовое сообщение по chat_id."""
    from src import notifier
    ok = notifier.send_telegram_message(chat_id, text)
    click.echo("OK" if ok else "FAIL — проверь TELEGRAM_BOT_TOKEN и chat_id")


@telegram_group.command("run-bot")
@click.option(
    "--timeout", type=int, default=30,
    help="Long-poll timeout (сек). На VPS ставь 30+",
)
def telegram_run_bot(timeout: int) -> None:
    """Запустить Telegram-бот в режиме long-polling.

    Слушает команды /start /today /alerts /status /help.
    Завершить: Ctrl+C. На VPS поднимается через systemd как отдельный сервис.
    """
    from src.telegram_bot import run_polling
    storage.init_db()
    click.echo("🤖 Telegram bot запущен. Ctrl+C для выхода.")
    run_polling(poll_timeout=timeout)


# ─── Notifications: digest commands (W9) ─────────────────────────────────────


@cli.group("notify")
def notify_group() -> None:
    """Notification dispatch — daily/weekly digest, manual sends."""


@notify_group.command("digest")
@click.argument("kind", type=click.Choice(["daily", "weekly"]))
@click.option("--tenant-id", type=int, default=1, help="Send digest for this tenant only")
@click.option("--dry-run", is_flag=True, help="Compute digest content but don't send emails")
def notify_digest(kind: str, tenant_id: int, dry_run: bool) -> None:
    """Send digest email to opted-in users for the tenant.

    Daily includes events from last 24h, weekly from last 7d. Recipients are
    `tenant_users` with daily_digest=True / weekly_digest=True.

    Schedule via systemd timer (see infra/systemd/pharmacy-monitor-digest@.timer).
    """
    from src import notifications

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        if dry_run:
            click.echo(f"[dry-run] would send {kind} digest for tenant_id={tenant_id}")
            return
        sent = (
            notifications.send_daily_digest(s, tenant_id=tenant_id)
            if kind == "daily"
            else notifications.send_weekly_digest(s, tenant_id=tenant_id)
        )
        click.echo(f"OK: {kind} digest sent to {sent} recipients")


@cli.command("ai-crawl")
@click.option(
    "--site",
    type=click.Choice(["aloe", "pharmonline", "aptekonline"]),
    required=True,
    help="Сайт для AI-обхода (sitemap + LLM extraction)",
)
@click.option(
    "--max-urls", type=int, default=200,
    help="Макс. URL за сессию (default 200; на больших сайтах >1000 = ~$1-3)",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Только discover URLs + heuristic-классификация, без LLM-extract (бесплатно).",
)
@click.option(
    "--budget-usd", type=float, default=None,
    help="Override env AI_CRAWL_BUDGET_USD. Crawl аборт когда лимит превышен.",
)
def ai_crawl_cmd(
    site: str, max_urls: int, dry_run: bool, budget_usd: float | None,
) -> None:
    """AI-driven crawl (Level 3) с LLM extraction.

    Discovery: /sitemap.xml + /robots.txt → set of URLs (cap 50K, фильтр по домену).
    Classify: heuristic (schema.org/Product, og:type=product, data-price), LLM fallback.
    Extract: gpt-4o-mini (default) → name/brand/price_azn/old_price_azn/pack_size/dosage/image_url.

    Provider/модель через env:
      AI_CRAWL_PROVIDER=openai|anthropic   (default openai)
      AI_CRAWL_MODEL=gpt-4o-mini|claude-haiku-4-5  (default gpt-4o-mini)
      OPENAI_API_KEY / ANTHROPIC_API_KEY     (required если не --dry-run)
      AI_CRAWL_BUDGET_USD=5.0                (default cap)
    """
    from src.scrapers.ai_crawler import AI_CRAWLER_BY_SITE

    if budget_usd is not None:
        os.environ["AI_CRAWL_BUDGET_USD"] = str(budget_usd)

    if not dry_run:
        provider = os.getenv("AI_CRAWL_PROVIDER", "openai").lower()
        key_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        if not os.getenv(key_var):
            raise click.ClickException(
                f"{key_var} не задан. Запусти с --dry-run или экспортируй ключ "
                f"(см. /etc/pharmacy-monitor/env на сервере)."
            )

    storage.init_db()

    async def _run() -> ScrapeResult:
        cls = AI_CRAWLER_BY_SITE[site]
        async with cls() as scraper:
            return await scraper.crawl(max_urls=max_urls, dry_run=dry_run)

    result = asyncio.run(_run())

    if dry_run:
        click.echo(
            f"[dry-run] AI-crawl {site}: errors={len(result.errors)}. "
            "См. structlog 'ai_crawl_summary' (logs/app.jsonl) для visited/captcha/cost."
        )
        return

    Session = storage.make_session()
    with Session() as session:
        run = storage.Run(status="running")
        session.add(run)
        session.commit()
        try:
            count = persist_results(session, run, [result])
            run.products_scraped = count
            run.products_per_site = {site: len(result.products)}
            run.products_per_site_category = {
                site: _per_category_breakdown([result])
            }
            run.sites_completed = site
            run.status = "ok"
            run.finished_at = utcnow()
            session.commit()
            click.echo(
                f"OK: AI-crawl {site} → run #{run.id}, "
                f"persisted {count} (из {len(result.products)} extracted), "
                f"errors={len(result.errors)}"
            )
        except Exception as e:
            run.status = "failed"
            run.error_message = f"{type(e).__name__}: {e}"
            run.finished_at = utcnow()
            session.commit()
            raise click.ClickException(str(e))


@cli.command("seed-demo")
@click.option("--force", is_flag=True, help="Стереть существующие Run/Product/Match данные")
def seed_demo_cmd(force: bool) -> None:
    """Подложить реалистичные fake-данные для демо/тестирования.

    Создаёт 30 товаров, 10 cross-site матчей, 2 прогона, 3 промо.
    Не трогает Categories / Recipients / TrackedProducts.
    """
    from src.demo import seed_demo

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        try:
            summary = seed_demo(s, force=force)
        except RuntimeError as e:
            raise click.ClickException(str(e))
    click.echo(
        f"OK: создано {summary['runs']} прогонов, {summary['products']} товаров, "
        f"{summary['matches']} матчей, {summary['promos']} промо. "
        "Открой 🔍 Сравнение цен в дашборде."
    )


@cli.command("run")
@click.option("--dry-run", is_flag=True, help="Не отправлять email, только сгенерировать отчёт в reports/")
@click.option("--limit", type=int, default=None, help="Ограничить N товаров на категорию (только в category-режиме)")
@click.option(
    "--site",
    multiple=True,
    type=click.Choice(list(SCRAPER_CLASSES.keys())),
    help="Скрейпить только указанные сайты (можно несколько)",
)
@click.option(
    "--mode",
    type=click.Choice(["auto", "watchlist", "category"]),
    default="auto",
    help="auto = watchlist если есть товары, иначе category. Можно форсировать.",
)
@click.option(
    "--category-id",
    type=int,
    default=None,
    help="Скрейпить ТОЛЬКО эту категорию (по id из таблицы categories). Только в category-режиме.",
)
@click.option(
    "--hourly", is_flag=True,
    help="Ежечасный микро-прогон: только watchlist + только pinned URL'ы. Быстрый.",
)
@click.option(
    "--no-alerts", is_flag=True,
    help="Пропустить evaluation алертов после прогона (по умолчанию запускается)",
)
@click.option(
    "--request-id",
    type=int,
    default=None,
    help="ID строки в scrape_requests. Если задан — после persist (но ДО matcher) "
         "немедленно проставляем status='ok' + run_id, чтобы UI показал «Готово — N "
         "товаров» не дожидаясь медленных matcher/analyzer фаз.",
)
def run_cmd(
    dry_run: bool, limit: int | None, site: tuple[str, ...], mode: str,
    category_id: int | None, hourly: bool, no_alerts: bool,
    request_id: int | None,
) -> None:
    """Полный прогон: scrape → match → analyze → report."""
    storage.init_db()
    sites = list(site) if site else list(SCRAPER_CLASSES.keys())

    Session = storage.make_session()
    with Session() as session:
        maybe_seed_categories(session)

        run = storage.Run(status="running")
        session.add(run)
        session.commit()
        run_id = run.id

        # --hourly → форсируем watchlist mode + skip categories + skip report email
        if hourly:
            mode = "watchlist"

        # Если задан --category-id → форсируем category-режим (даже если watchlist непустой)
        if category_id is not None and mode == "auto":
            mode = "category"

        # Определяем режим
        watchlist_urls = collect_watchlist_urls(session) if mode != "category" else {}
        total_pinned = sum(len(urls) for urls in watchlist_urls.values())
        if mode == "watchlist" or (mode == "auto" and total_pinned > 0):
            effective_mode = "watchlist"
        else:
            effective_mode = "category"

        log.info(
            "run_started",
            run_id=run_id,
            sites=sites,
            limit=limit,
            dry_run=dry_run,
            mode=effective_mode,
            watchlist_pinned=total_pinned,
            category_id=category_id,
        )

        try:
            if effective_mode == "watchlist":
                filtered = {s: urls for s, urls in watchlist_urls.items() if s in sites}
                results = asyncio.run(scrape_watchlist_all(filtered))
            else:
                # Категории из БД, опционально фильтр по одной category_id
                slugs_by_site = {
                    s: watchlist.categories_for_site(session, s, only_category_id=category_id)
                    for s in sites
                }
                results = asyncio.run(scrape_all(slugs_by_site, limit))
            count = persist_results(session, run, results)
            run.products_scraped = count
            run.products_per_site = {r.site: len(r.products) for r in results}
            run.products_per_site_category = {
                r.site: _per_category_breakdown([r]) for r in results
            }
            run.sites_completed = ",".join(sites)
            session.commit()

            # === Early-complete для UI-triggered scrape (job queue) ===
            # Если запущены через `pharmacy-monitor run --request-id N`, помечаем
            # ScrapeRequest как 'ok' СРАЗУ после persist. Это даёт UI feedback
            # «Готово — N товаров» в течение секунды после scrape phase, не
            # заставляя клиента ждать 10-30 мин на matcher через SSH tunnel.
            # Matcher/analyzer запустятся дальше, но клиент уже видит результат.
            if request_id is not None:
                req = session.get(storage.ScrapeRequest, request_id)
                if req is not None:
                    req.run_id = run.id
                    req.status = "ok"
                    req.completed_at = datetime.utcnow()
                    session.commit()
                    log.info(
                        "scrape_request_marked_ok_early",
                        request_id=request_id,
                        run_id=run.id,
                        products_scraped=count,
                    )

            # === Smoke-test: per-site coverage drop ===
            # Если конкретный сайт собрал <50% от среднего за последние 5 ok-runs —
            # пишем warning в alert_events. Защищает от silent regressions.
            try:
                _smoke_test_per_site_coverage(session, results, run)
            except Exception as e:
                log.warning("smoke_test_failed", error=str(e))

            if effective_mode == "watchlist":
                linked = auto_match_watchlist(session)
                log.info("watchlist_auto_matched", linked=linked)

            # AI-нормализация фармацевтических атрибутов перед matcher.
            # Feature flag: PHARMACY_AI_NORMALIZE=0 отключает (default on).
            # При отсутствии ANTHROPIC_API_KEY модуль fail-soft — matcher
            # переключается на legacy fuzzy path для unnormalized продуктов.
            if os.getenv("PHARMACY_AI_NORMALIZE", "1") == "1" and not dry_run:
                try:
                    from src import ai_normalize
                    stats = ai_normalize.normalize_run(session)
                    log.info(
                        "ai_normalize_done",
                        total=stats.products_total,
                        called=stats.products_called,
                        cached=stats.products_cached,
                        failed=stats.products_failed,
                        cost_usd=round(stats.cost_usd, 4),
                        budget_exceeded=stats.budget_exceeded,
                    )
                except Exception as e:
                    log.warning("ai_normalize_skipped", error=str(e))

            matcher.match_products(session)

            # === Real-time alerts ===
            if not no_alerts:
                from src import alerts as alerts_mod
                fired = alerts_mod.evaluate_rules(session, run.id)
                if fired and not dry_run:
                    for ev in fired:
                        try:
                            alerts_mod.dispatch_event(session, ev)
                        except Exception as e:
                            log.warning("alert_dispatch_failed", error=str(e))
                    log.info("alerts_dispatched", count=len(fired))
                elif fired:
                    log.info("alerts_dispatch_skipped_dry_run", count=len(fired))
            report = analyzer.analyze(session, run.id)

            html = reporter.render_html(report)
            xlsx = reporter.render_excel(report)
            subject = reporter.email_subject(report)

            reports_dir = Path("reports")
            reports_dir.mkdir(exist_ok=True)
            stamp = report.run_started_at.strftime("%Y-%m-%d_%H%M")
            html_path = reports_dir / f"report-{stamp}.html"
            xlsx_path = reports_dir / reporter.excel_filename(report)
            html_path.write_text(html, encoding="utf-8")
            xlsx_path.write_bytes(xlsx)
            log.info("report_saved", html=str(html_path), xlsx=str(xlsx_path))

            # В hourly режиме пропускаем большой email-отчёт (только alerts).
            # Любая ошибка отправки (SMTP quota, бан, network) НЕ должна валить
            # весь run — скрейп уже завершён, данные в БД. Просто пишем warning.
            if not dry_run and not hourly:
                try:
                    notifier.send_email(
                        subject=subject,
                        html_body=html,
                        attachments=[
                            (
                                reporter.excel_filename(report),
                                xlsx,
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            )
                        ],
                    )
                except Exception as email_err:
                    log.warning(
                        "report_email_failed",
                        run_id=run_id,
                        error=str(email_err),
                    )

            run.status = "ok"
            run.finished_at = utcnow()
            session.commit()
            log.info("run_ok", run_id=run_id, products=count)

            # P0.1 (PO Audit 2026-05-17): pre-compute ROI actions для всех 3
            # сайтов и сохранить в roi_actions_cache. HTTP-handler
            # /dash/roi/actions читает оттуда → <50мс latency вместо
            # 15-30с inline compute (timeout'ило с 408 на 4 экранах).
            # Fail-soft — ошибка не валит run, max 5-10с overhead на пересчёт.
            try:
                from src import roi as _roi
                summary = _roi.refresh_all_cached_actions(session, run_id=run_id)
                log.info("roi_cache_refreshed", run_id=run_id, **summary)
            except Exception as cache_err:
                log.warning(
                    "roi_cache_refresh_failed",
                    run_id=run_id,
                    error=str(cache_err),
                )
        except Exception as e:
            run.status = "failed"
            run.error_message = f"{type(e).__name__}: {e}"
            run.finished_at = utcnow()
            session.commit()
            log.exception("run_failed", run_id=run_id)
            raise click.ClickException(str(e))


@cli.command("scrape")
@click.option("--limit", type=int, default=None)
@click.option("--site", multiple=True, type=click.Choice(list(SCRAPER_CLASSES.keys())))
def scrape_cmd(limit: int | None, site: tuple[str, ...]) -> None:
    """Только скрейпинг — без анализа и отправки."""
    storage.init_db()
    sites = list(site) if site else list(SCRAPER_CLASSES.keys())

    Session = storage.make_session()
    with Session() as session:
        maybe_seed_categories(session)
        run = storage.Run(status="running")
        session.add(run)
        session.commit()
        try:
            slugs_by_site = {s: watchlist.categories_for_site(session, s) for s in sites}
            results = asyncio.run(scrape_all(slugs_by_site, limit))
            count = persist_results(session, run, results)
            run.products_scraped = count
            run.products_per_site = {r.site: len(r.products) for r in results}
            run.products_per_site_category = {
                r.site: _per_category_breakdown([r]) for r in results
            }
            run.status = "ok"
            run.finished_at = utcnow()
            session.commit()
            click.echo(f"Scraped {count} products in run #{run.id}")
        except Exception as e:
            run.status = "failed"
            run.error_message = str(e)
            session.commit()
            raise click.ClickException(str(e))


@cli.command("report")
@click.option("--run-id", type=int, default=None, help="ID прогона (по умолчанию — последний ok)")
@click.option("--send", is_flag=True, help="Отправить по email")
def report_cmd(run_id: int | None, send: bool) -> None:
    """Перегенерировать отчёт по существующему прогону."""
    Session = storage.make_session()
    with Session() as session:
        if run_id is None:
            run = session.scalars(
                select(storage.Run)
                .where(storage.Run.status == "ok")
                .order_by(storage.Run.id.desc())
                .limit(1)
            ).first()
            if not run:
                raise click.ClickException("No successful runs found.")
            run_id = run.id

        report = analyzer.analyze(session, run_id)
        html = reporter.render_html(report)
        xlsx = reporter.render_excel(report)

        reports_dir = Path("reports")
        reports_dir.mkdir(exist_ok=True)
        stamp = report.run_started_at.strftime("%Y-%m-%d_%H%M")
        (reports_dir / f"report-{stamp}.html").write_text(html, encoding="utf-8")
        (reports_dir / reporter.excel_filename(report)).write_bytes(xlsx)
        click.echo(f"Saved report for run #{run_id} to reports/")

        if send:
            notifier.send_email(
                subject=reporter.email_subject(report),
                html_body=html,
                attachments=[
                    (
                        reporter.excel_filename(report),
                        xlsx,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                ],
            )
            click.echo("Email sent.")


# ============================================================================
# CATEGORIES — CRUD категорий для category-режима
# ============================================================================


@cli.group("category")
def category_group() -> None:
    """Управление категориями (для category-режима скрейпинга)."""


@category_group.command("add")
@click.argument("key")
@click.argument("label_ru")
@click.option("--label-az", default=None)
@click.option("--pharmonline-slug", default=None)
@click.option("--aptekonline-slug", default=None)
@click.option("--aloe-slug", default=None)
def category_add(
    key: str,
    label_ru: str,
    label_az: str | None,
    pharmonline_slug: str | None,
    aptekonline_slug: str | None,
    aloe_slug: str | None,
) -> None:
    """Добавить или обновить категорию."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        cat = watchlist.add_category(
            s,
            key=key,
            label_ru=label_ru,
            label_az=label_az,
            pharmonline_slug=pharmonline_slug,
            aptekonline_slug=aptekonline_slug,
            aloe_slug=aloe_slug,
        )
        slugs = (
            f"ph={cat.pharmonline_slug or '—'} ap={cat.aptekonline_slug or '—'} "
            f"al={cat.aloe_slug or '—'}"
        )
        click.echo(f"OK: #{cat.id} {cat.key} ({cat.label_ru}) — {slugs}")


@category_group.command("list")
@click.option("--all", "show_all", is_flag=True, help="Включая деактивированные")
def category_list(show_all: bool) -> None:
    """Список категорий."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        maybe_seed_categories(s)
        rows = watchlist.list_categories(s, active_only=not show_all)
        if not rows:
            click.echo("(нет категорий — добавь через `category add` или `category seed`)")
            return
        for cat in rows:
            mark = "✓" if cat.is_active else "✗"
            click.echo(
                f"  [{mark}] #{cat.id:3d} {cat.key:30s} {cat.label_ru[:30]:30s}  "
                f"ph={cat.pharmonline_slug or '—':25s} "
                f"ap={cat.aptekonline_slug or '—':10s} "
                f"al={cat.aloe_slug or '—'}"
            )


@category_group.command("remove")
@click.argument("cat_id", type=int)
def category_remove(cat_id: int) -> None:
    """Удалить категорию."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        ok = watchlist.remove_category(s, cat_id)
        click.echo("OK: removed" if ok else f"Not found: #{cat_id}")


@category_group.command("toggle")
@click.argument("cat_id", type=int)
def category_toggle(cat_id: int) -> None:
    """Активировать/деактивировать без удаления."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        cat = watchlist.toggle_category(s, cat_id)
        if not cat:
            click.echo(f"Not found: #{cat_id}")
            return
        click.echo(f"OK: #{cat.id} {cat.key} is_active={cat.is_active}")


@category_group.command("seed")
def category_seed() -> None:
    """Импорт категорий из config/categories.yaml."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        n = watchlist.seed_categories_from_yaml(s, CONFIG_PATH)
        click.echo(f"OK: seeded {n} categories from {CONFIG_PATH}")


# ============================================================================
# RECIPIENTS — CRUD получателей email-рассылки
# ============================================================================


@cli.group("recipient")
def recipient_group() -> None:
    """Управление получателями email-рассылки."""


@recipient_group.command("add")
@click.argument("email")
@click.option("--name", default=None, help="Имя получателя (опционально)")
def recipient_add(email: str, name: str | None) -> None:
    """Добавить или активировать получателя."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        r = watchlist.add_recipient(s, email, name)
        click.echo(f"OK: {r.email} ({r.name or '—'}) is_active={r.is_active}")


@recipient_group.command("list")
@click.option("--active-only", is_flag=True, help="Только активные")
def recipient_list(active_only: bool) -> None:
    """Список получателей."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        rows = watchlist.list_recipients(s, active_only=active_only)
        if not rows:
            click.echo("(нет получателей)")
            return
        for r in rows:
            mark = "✓" if r.is_active else "✗"
            click.echo(f"  [{mark}] {r.email}  {r.name or ''}")


@recipient_group.command("remove")
@click.argument("email")
def recipient_remove(email: str) -> None:
    """Удалить получателя."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        ok = watchlist.remove_recipient(s, email)
        click.echo("OK: removed" if ok else f"Not found: {email}")


@recipient_group.command("toggle")
@click.argument("email")
def recipient_toggle(email: str) -> None:
    """Активировать/деактивировать получателя без удаления."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        r = watchlist.toggle_recipient(s, email)
        if not r:
            click.echo(f"Not found: {email}")
            return
        click.echo(f"OK: {r.email} is_active={r.is_active}")


@recipient_group.command("update")
@click.argument("email")
@click.option("--new-email", default=None, help="Новый email")
@click.option("--name", default=None, help="Новое имя")
def recipient_update(email: str, new_email: str | None, name: str | None) -> None:
    """Изменить email или имя существующей записи."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        r = watchlist.update_recipient(s, email, new_email=new_email, name=name)
        if not r:
            click.echo(f"Not found: {email}")
            return
        click.echo(f"OK: {r.email} ({r.name or '—'})")


# ============================================================================
# WATCHLIST — CRUD конкретных отслеживаемых SKU
# ============================================================================


@cli.group("watchlist")
def watchlist_group() -> None:
    """Управление списком конкретных товаров для отслеживания."""


@watchlist_group.command("add")
@click.argument("name")
@click.option("--brand", default=None)
@click.option("--dosage", default=None)
@click.option("--pack-size", default=None)
@click.option("--search-query", default=None, help="Текст для site search")
@click.option("--pharmonline-url", default=None)
@click.option("--aptekonline-url", default=None)
@click.option("--aloe-url", default=None)
@click.option("--notes", default=None)
def watchlist_add(name: str, **kw) -> None:
    """Добавить товар в watchlist."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        tp = watchlist.add_tracked_product(s, name, **kw)
        click.echo(f"OK: tracked #{tp.id} — {tp.canonical_name}")


@watchlist_group.command("list")
@click.option("--all", "show_all", is_flag=True, help="Включая деактивированные")
def watchlist_list(show_all: bool) -> None:
    """Список отслеживаемых товаров."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        rows = watchlist.list_tracked(s, active_only=not show_all)
        if not rows:
            click.echo("(watchlist пуст — добавь товары через `watchlist add` или `watchlist import`)")
            return
        for tp in rows:
            mark = "✓" if tp.is_active else "✗"
            urls = {link.site: link.url for link in tp.links}
            confirmed = sum(1 for link in tp.links if link.status == "confirmed" and link.url)
            click.echo(
                f"  [{mark}] #{tp.id} {tp.canonical_name}"
                + (f" ({tp.brand})" if tp.brand else "")
                + (f" {tp.dosage}" if tp.dosage else "")
                + f" — {confirmed}/3 sites linked"
            )


@watchlist_group.command("remove")
@click.argument("tracked_id", type=int)
def watchlist_remove(tracked_id: int) -> None:
    """Удалить запись из watchlist."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        ok = watchlist.remove_tracked(s, tracked_id)
        click.echo("OK: removed" if ok else f"Not found: #{tracked_id}")


@watchlist_group.command("link")
@click.argument("tracked_id", type=int)
@click.option(
    "--site", required=True, type=click.Choice(list(watchlist.SITES))
)
@click.option("--url", required=True)
@click.option("--status", default="confirmed", type=click.Choice(["pending", "confirmed", "not_found"]))
def watchlist_link(tracked_id: int, site: str, url: str, status: str) -> None:
    """Привязать конкретный URL к товару на конкретном сайте."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        link = watchlist.set_link_url(s, tracked_id, site, url, status=status)
        if not link:
            click.echo(f"Tracked product #{tracked_id} not found")
            return
        click.echo(f"OK: tracked #{tracked_id} on {site} → {url} ({status})")


@watchlist_group.command("import")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False))
def watchlist_import(csv_path: str) -> None:
    """Массовый импорт из CSV."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        n = watchlist.import_from_csv(s, Path(csv_path))
        click.echo(f"OK: imported {n} products from {csv_path}")


@watchlist_group.command("export")
@click.argument("csv_path", type=click.Path(dir_okay=False))
def watchlist_export(csv_path: str) -> None:
    """Экспорт текущего watchlist в CSV (для редактирования вручную)."""
    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        n = watchlist.export_to_csv(s, Path(csv_path))
        click.echo(f"OK: exported {n} products to {csv_path}")


if __name__ == "__main__":
    cli()
