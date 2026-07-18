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
from datetime import timedelta
from src._time import utcnow
from pathlib import Path

import click
import structlog
import yaml
from dotenv import load_dotenv
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

load_dotenv(override=True)

from src import analyzer, matcher, notifier, reporter, storage, watchlist  # noqa: E402
from src.run_lock import SCRAPE_ADVISORY_LOCK_KEY  # noqa: E402
from src.scrapers.ai_crawler import AI_CRAWLER_BY_SITE  # noqa: E402
from src.scrapers.aloe import AloeScraper  # noqa: E402
from src.scrapers.aptekonline import AptekonlineScraper  # noqa: E402
from src.scrapers.base import (  # noqa: E402
    BaseScraper,
    ScrapedProduct,
    ScrapeResult,
    SiteScrapeFatalError,
    fatal_proxy_reason,
    site_fatal_error_message,
    site_fatal_result,
)
from src.scrapers.pharmonline import PharmonlineScraper  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "categories.yaml"


class FullCatalogVerificationError(RuntimeError):
    """A nominal full scan did not prove complete item-level coverage."""


# Phase 1c (2026-05-27) — pharmonline DDP path uses reverse-engineered Meteor
# protocol через WebSocket, обходит Cloudflare без Playwright. Opt-in via
# PHARMONLINE_USE_DDP=1. Когда выключено — используется legacy Playwright путь.
def _pharmonline_scraper_class() -> type[BaseScraper]:
    if os.environ.get("PHARMONLINE_USE_DDP", "").lower() in ("1", "true", "yes"):
        from src.scrapers.pharmonline_ddp import PharmonlineDDPScraper

        return PharmonlineDDPScraper
    return PharmonlineScraper


SCRAPER_CLASSES: dict[str, type[BaseScraper]] = {
    "pharmonline": _pharmonline_scraper_class(),
    "aptekonline": AptekonlineScraper,
    "aloe": AloeScraper,
}

# Phase 1.4 (2026-05-27) — AI crawler fallback orchestration.
#
# Primary scrapers (Playwright DOM / httpx JSON) могут отдать ~0 продуктов из-за
# IP-бана, ротации anti-bot, captcha. В таком случае пробуем AICrawler через
# sitemap-discovery + LLM extraction (платный, ~$5/site/run при cold sitemap).
#
# Триггер настраивается через env (умолчания агрессивно-консервативные):
#   AI_FALLBACK_ENABLED=1        включает (default off, чтобы не жечь Claude API)
#   AI_FALLBACK_RATIO=0.5        retry если primary yield < 50% от baseline
#   AI_FALLBACK_MIN_BASELINE=100 baseline ниже — игнор (слишком ненадёжный сигнал)
AI_FALLBACK_ENABLED_ENV = "AI_FALLBACK_ENABLED"
AI_FALLBACK_RATIO_ENV = "AI_FALLBACK_RATIO"
AI_FALLBACK_MIN_BASELINE_ENV = "AI_FALLBACK_MIN_BASELINE"
AI_FALLBACK_MAX_URLS_ENV = "AI_FALLBACK_MAX_URLS"


def _ai_fallback_enabled() -> bool:
    return os.environ.get(AI_FALLBACK_ENABLED_ENV, "").lower() in ("1", "true", "yes")


def _should_trigger_ai_fallback(primary_yield: int, baseline: int | None) -> bool:
    """Decide whether to invoke AI fallback after primary scraper returned.

    - Disabled by env → never
    - No baseline / baseline too small → don't trigger (signal unreliable)
    - primary_yield >= ratio * baseline → primary was good enough
    """
    if not _ai_fallback_enabled():
        return False
    if baseline is None:
        return False
    try:
        min_baseline = int(os.environ.get(AI_FALLBACK_MIN_BASELINE_ENV, "100"))
    except ValueError:
        min_baseline = 100
    if baseline < min_baseline:
        return False
    try:
        ratio = float(os.environ.get(AI_FALLBACK_RATIO_ENV, "0.5"))
    except ValueError:
        ratio = 0.5
    threshold = int(baseline * ratio)
    return primary_yield < threshold


# ── Полный email-отчёт скрейпа (reporter.py) — opt-out ────────────────────────
#
# HTML-отчёт + Excel-вложение (тема «Pharmacy Monitor DD.MM.YYYY — N undercuts»)
# шлётся в конце каждого НЕ-hourly прогона на EMAIL_TO/recipients. Это отдельный
# канал от мгновенных undercut-алертов (evaluate_rules → dispatch_event) и от
# daily/weekly дайджестов — управляется собственным флагом:
#   SCRAPE_REPORT_EMAIL=0   отключает письмо (default ON — обратная совместимость)
#
# NB полярность: это opt-OUT (default ON, гасим явным falsy) — в отличие от
# соседнего _ai_fallback_enabled, который opt-IN (default OFF, включаем явным
# truthy). Разные дефолты намеренны: отчёт исторически слался всегда (не ломаем
# существующее поведение), а AI-fallback жжёт деньги → по умолчанию выключен.
SCRAPE_REPORT_EMAIL_ENV = "SCRAPE_REPORT_EMAIL"


def _report_email_enabled() -> bool:
    """Слать ли полный email-отчёт скрейпа. Opt-out через SCRAPE_REPORT_EMAIL=0.

    Мгновенные undercut-алерты идут отдельным путём и этим флагом НЕ управляются.
    """
    return os.environ.get(SCRAPE_REPORT_EMAIL_ENV, "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


_SCRAPE_ADVISORY_LOCK_KEY = SCRAPE_ADVISORY_LOCK_KEY
def _acquire_matcher_lock(session: Session, *, wait: bool) -> bool:
    """Serialize matcher/rematch writes to products.canonical_id on Postgres."""
    return matcher.acquire_match_mutation_lock(session, wait=wait)


def _release_matcher_lock(session: Session) -> None:
    try:
        matcher.release_match_mutation_lock(session)
    except Exception as exc:
        log.warning("matcher_lock_release_failed", error=str(exc))


def _hold_scrape_lock_until_command_exit(SessionFactory, *, wait: bool) -> bool:
    """Hold one checked-out connection's session lock without an idle transaction."""
    bind = SessionFactory.kw.get("bind")
    if bind is None:
        raise RuntimeError("scrape lock requires a bound session factory")
    if not str(bind.url).startswith("postgresql"):
        return True

    connection = bind.connect()
    try:
        function = "pg_advisory_lock" if wait else "pg_try_advisory_lock"
        acquired = connection.scalar(
            text(f"SELECT {function}(hashtext(:key))"),
            {"key": _SCRAPE_ADVISORY_LOCK_KEY},
        )
        # Session-level advisory locks survive COMMIT. End the implicit
        # transaction immediately so a multi-hour scrape is never
        # idle-in-transaction, while this exact connection stays checked out.
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        raise
    if not wait and not acquired:
        connection.close()
        return False

    context = click.get_current_context(silent=True)
    if context is None:
        try:
            connection.scalar(
                text("SELECT pg_advisory_unlock(hashtext(:key))"),
                {"key": _SCRAPE_ADVISORY_LOCK_KEY},
            )
            connection.commit()
        finally:
            connection.close()
        raise RuntimeError("scrape lock requires an active Click command context")

    def release() -> None:
        try:
            connection.scalar(
                text("SELECT pg_advisory_unlock(hashtext(:key))"),
                {"key": _SCRAPE_ADVISORY_LOCK_KEY},
            )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            log.warning("scrape_lock_release_failed", error=str(exc))
        finally:
            connection.close()

    context.call_on_close(release)
    return True


def baselines_for_sites(session: Session, sites: list[str]) -> dict[str, int | None]:
    """Pre-fetch products_per_site from latest ok run for each requested site.

    Используется в run_cmd для опционального AI fallback'а. Возвращает {} если
    раньше не было успешного прогона — fallback не активируется на первом запуске.
    """
    out: dict[str, int | None] = {site: None for site in sites}
    last_ok = session.scalars(
        select(storage.Run)
        .where(storage.Run.status == "ok")
        .order_by(desc(storage.Run.id))
        .limit(1)
    ).first()
    if last_ok is None or not last_ok.products_per_site:
        return out
    for site in sites:
        val = last_ok.products_per_site.get(site)
        if isinstance(val, int) and val > 0:
            out[site] = val
    return out


def run_quality_baselines_for_sites(
    session: Session,
    sites: list[str],
    *,
    tenant_id: int = 1,
    history_limit: int = 100,
) -> dict[str, int | None]:
    """Return the median of recent confirmed full-run counts per site.

    New rows explicitly carry ``financially_eligible``. Legacy rows are only
    accepted when they contain a multi-category breakdown; this rejects hourly
    ticks/watchlists without letting an old inflated maximum poison the
    baseline forever.
    """
    candidates: dict[str, list[int]] = {site: [] for site in sites}
    recent = session.scalars(
        select(storage.Run)
        .where(
            storage.Run.status == "ok",
            storage.Run.tenant_id == tenant_id,
        )
        .order_by(desc(storage.Run.id))
        .limit(history_limit)
    ).all()
    for run in recent:
        quality = run.run_quality or {}
        per_site = run.products_per_site or {}
        for site in sites:
            if quality:
                if not quality.get("financially_eligible"):
                    continue
            else:
                legacy_categories = (run.products_per_site_category or {}).get(site) or {}
                if len(legacy_categories) < 2:
                    continue
            value = per_site.get(site)
            if isinstance(value, int) and value > 0:
                candidates[site].append(value)
        if all(len(values) >= 5 for values in candidates.values()):
            break

    out: dict[str, int | None] = {}
    for site, values in candidates.items():
        if not values:
            out[site] = None
            continue
        sample = sorted(values[:5])
        midpoint = len(sample) // 2
        if len(sample) % 2:
            out[site] = sample[midpoint]
        else:
            out[site] = round((sample[midpoint - 1] + sample[midpoint]) / 2)
    return out


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


def reap_stale_running_runs(
    session: Session,
    *,
    max_age_hours: float = 6.0,
    reason: str = "reaped stale unfinished run after interrupted/timeout process",
) -> int:
    """Fail and finish every stale run left without ``finished_at``.

    Callers must guard that no pharmacy-monitor scrape/rematch process is active.
    This is for DB rows left behind after a killed process, reboot, timeout, or
    a legacy error path that set ``status=failed`` without a completion stamp.
    A classified ``ok``/``degraded`` orphan is deliberately stripped of money
    trust: post-processing may have stopped after mutating Product/Match rows.
    """
    cutoff = utcnow() - timedelta(hours=max_age_hours)
    stale = session.scalars(
        select(storage.Run)
        .where(
            storage.Run.started_at < cutoff,
            storage.Run.finished_at.is_(None),
        )
        .order_by(storage.Run.started_at)
    ).all()
    if not stale:
        return 0
    recovered_at = utcnow()
    for run in stale:
        previous_status = run.status
        run.status = "failed"
        # Preserve historical ordering. Recovery today must not make a May
        # orphan newer than a healthy July run merely because its legacy row
        # lacked a completion stamp.
        run.finished_at = run.started_at
        if run.run_quality:
            quality = dict(run.run_quality)
            quality["full_catalog_verified"] = False
            quality["financially_eligible"] = False
            quality["recovery"] = {
                "reason": reason,
                "previous_status": previous_status,
                "recovered_at": recovered_at.isoformat(),
            }
            run.run_quality = quality
        recovery_note = (
            f"{reason} (previous_status={previous_status}, recovered_at={recovered_at.isoformat()})"
        )
        run.error_message = ((run.error_message or "") + f" | {recovery_note}").strip(" |")
    session.commit()
    return len(stale)


def count_duplicate_products(session: Session) -> int:
    """Count duplicate `(site, external_id)` product groups."""
    duplicate_groups = (
        select(storage.Product.site, storage.Product.external_id)
        .group_by(storage.Product.site, storage.Product.external_id)
        .having(func.count(storage.Product.id) > 1)
        .subquery()
    )
    return session.scalar(select(func.count()).select_from(duplicate_groups)) or 0


async def scrape_site(
    site: str,
    slugs: list[str],
    limit_per_category: int | None,
    *,
    ai_fallback_baseline: int | None = None,
    on_category=None,
    aloe_country_map: dict[str, dict[str, object]] | None = None,
) -> ScrapeResult:
    cls = SCRAPER_CLASSES[site]
    if not slugs:
        log.warning("no_categories_configured", site=site)
        return ScrapeResult(
            site=site,
            errors=["no_categories_configured"],
        )
    scraper_kwargs = (
        {"country_id_map": aloe_country_map or {}} if site == "aloe" else {}
    )
    try:
        async with cls(**scraper_kwargs) as s:
            result = await s.scrape(
                slugs, limit_per_category=limit_per_category, on_category=on_category
            )
            if site == "aloe":
                result.verified_country_mappings.update(
                    getattr(s, "verified_country_mappings", {})
                )
    except SiteScrapeFatalError as exc:
        log.error(
            "site_scrape_start_aborted",
            site=site,
            categories=len(slugs),
            error=site_fatal_error_message(exc),
        )
        return site_fatal_result(site, slugs, exc)

    # Phase 1.4 — optional AI crawler fallback when primary yield collapses.
    # Only kicks in if AI_FALLBACK_ENABLED=1 in env (off by default — costs $).
    if not result.site_fatal and _should_trigger_ai_fallback(
        len(result.products), ai_fallback_baseline
    ):
        ai_cls = AI_CRAWLER_BY_SITE.get(site)
        if ai_cls is None:
            log.warning("ai_fallback_no_subclass", site=site)
        else:
            log.warning(
                "ai_fallback_triggered",
                site=site,
                primary_yield=len(result.products),
                baseline=ai_fallback_baseline,
            )
            try:
                max_urls = int(os.environ.get(AI_FALLBACK_MAX_URLS_ENV, "500"))
            except ValueError:
                max_urls = 500
            try:
                async with ai_cls() as ai_s:
                    ai_result = await ai_s.crawl(max_urls=max_urls)
                # Merge: skip dups by external_id (primary wins).
                seen_ext = {p.external_id for p in result.products}
                added = 0
                for p in ai_result.products:
                    if p.external_id not in seen_ext:
                        result.products.append(p)
                        seen_ext.add(p.external_id)
                        added += 1
                log.info(
                    "ai_fallback_merged",
                    site=site,
                    primary=len(result.products) - added,
                    ai_added=added,
                    ai_errors=len(ai_result.errors),
                )
                if ai_result.errors:
                    result.errors.extend(f"ai_fallback: {e}" for e in ai_result.errors[:5])
            except SiteScrapeFatalError as e:
                message = site_fatal_error_message(e)
                result.site_fatal = True
                result.errors.append(f"site_fatal: ai_fallback: {message}")
                log.error("ai_fallback_aborted", site=site, error=message)
            except Exception as e:
                reason = fatal_proxy_reason(e)
                if reason is not None:
                    result.site_fatal = True
                    result.errors.append(f"site_fatal: ai_fallback: {reason}")
                    log.error("ai_fallback_aborted", site=site, error=reason)
                else:
                    log.error(
                        "ai_fallback_failed",
                        site=site,
                        error=f"{type(e).__name__}: {e}",
                    )
                    result.errors.append(f"ai_fallback: {type(e).__name__}: {e}")
    return result


async def scrape_all(
    sites_with_slugs: dict[str, list[str]],
    limit_per_category: int | None,
    *,
    ai_fallback_baselines: dict[str, int | None] | None = None,
    on_category=None,
    aloe_country_map: dict[str, dict[str, object]] | None = None,
) -> list[ScrapeResult]:
    baselines = ai_fallback_baselines or {}
    tasks = [
        scrape_site(
            site,
            slugs,
            limit_per_category,
            ai_fallback_baseline=baselines.get(site),
            on_category=on_category,
            aloe_country_map=aloe_country_map,
        )
        for site, slugs in sites_with_slugs.items()
    ]
    return await asyncio.gather(*tasks)


def load_aloe_country_map(session: Session, *, tenant_id: int = 1) -> dict[str, dict[str, object]]:
    rows = session.scalars(
        select(storage.AloeCountryMapping).where(
            storage.AloeCountryMapping.tenant_id == tenant_id
        )
    ).all()
    return {
        row.country_id: {
            "country_code": row.country_code,
            "country_raw": row.country_raw,
            "source_url": row.source_url,
            "sample_count": row.sample_count,
            "version": row.version,
        }
        for row in rows
    }


def persist_aloe_country_mappings(
    session: Session,
    results: list[ScrapeResult],
    *,
    tenant_id: int = 1,
) -> int:
    """Upsert detail-verified Aloe country dictionary discoveries."""
    changed = 0
    for result in results:
        if result.site != "aloe":
            continue
        for country_id, mapping in result.verified_country_mappings.items():
            country_code = str(mapping.get("country_code") or "").lower()
            country_raw = str(mapping.get("country_raw") or "").strip()
            source_url = str(mapping.get("source_url") or "").strip()
            if not country_code or not country_raw or not source_url:
                continue
            row = session.scalar(
                select(storage.AloeCountryMapping).where(
                    storage.AloeCountryMapping.tenant_id == tenant_id,
                    storage.AloeCountryMapping.country_id == str(country_id),
                )
            )
            if row is None:
                row = storage.AloeCountryMapping(
                    tenant_id=tenant_id,
                    country_id=str(country_id),
                    country_code=country_code,
                    country_raw=country_raw,
                    source_url=source_url,
                    sample_count=int(mapping.get("sample_count") or 1),
                    version=1,
                    verified_at=utcnow(),
                )
                session.add(row)
                changed += 1
            elif row.country_code != country_code or row.country_raw != country_raw:
                row.country_code = country_code
                row.country_raw = country_raw
                row.source_url = source_url
                row.sample_count = int(mapping.get("sample_count") or 1)
                row.version += 1
                row.verified_at = utcnow()
                changed += 1
    if changed:
        session.flush()
    return changed


async def scrape_watchlist_for_site(site: str, urls: list[str]) -> ScrapeResult:
    """Watchlist-режим: ходим по конкретным URL'ам товаров на одном сайте."""
    cls = SCRAPER_CLASSES[site]
    if not urls:
        return ScrapeResult(site=site, errors=["no_watchlist_urls"])
    result = ScrapeResult(site=site, items_expected=len(urls))
    try:
        async with cls() as s:

            def _record_url(url, product, error):
                if product is not None:
                    result.products.append(product)
                    result.items_completed += 1
                    result.item_results[url] = {"status": "ok", "products": 1}
                    return
                result.items_failed += 1
                result.item_results[url] = {
                    "status": "failed",
                    "products": 0,
                    "error": str(error or "product_not_found")[:500],
                    "error_kind": ("not_found" if error == "product_not_found" else "exception"),
                }
                result.errors.append(f"url={url}: {error or 'product_not_found'}")

            def _record_abort(current_url, remaining_urls, error):
                message = site_fatal_error_message(error)
                result.site_fatal = True
                result.items_failed += 1 + len(remaining_urls)
                result.item_results[current_url] = {
                    "status": "failed",
                    "products": 0,
                    "error": message,
                    "error_kind": "site_fatal",
                }
                for remaining_url in remaining_urls:
                    result.item_results[remaining_url] = {
                        "status": "skipped",
                        "products": 0,
                        "error": message,
                        "error_kind": "site_fatal",
                    }
                result.errors.append(f"site_fatal: {message}")

            try:
                await s.scrape_urls(urls, on_result=_record_url, on_abort=_record_abort)
            except SiteScrapeFatalError:
                return result
            try:
                result.promos = await s.scrape_promos()
            except SiteScrapeFatalError as e:
                message = site_fatal_error_message(e)
                result.site_fatal = True
                result.errors.append(f"site_fatal: promos: {message}")
                log.error("watchlist_promos_aborted", site=site, error=message)
            except Exception as e:
                reason = fatal_proxy_reason(e)
                if reason is not None:
                    message = reason
                    result.site_fatal = True
                    result.errors.append(f"site_fatal: promos: {message}")
                    log.error("watchlist_promos_aborted", site=site, error=message)
                else:
                    log.warning("promos_failed", site=site, error=str(e))
                    result.errors.append(f"promos: {type(e).__name__}: {e}")
            return result
    except SiteScrapeFatalError as exc:
        log.error(
            "watchlist_site_start_aborted",
            site=site,
            urls=len(urls),
            error=site_fatal_error_message(exc),
        )
        return site_fatal_result(site, urls, exc)


async def scrape_watchlist_all(urls_by_site: dict[str, list[str]]) -> list[ScrapeResult]:
    tasks = [scrape_watchlist_for_site(site, urls) for site, urls in urls_by_site.items()]
    return await asyncio.gather(*tasks)


def collect_watchlist_urls(
    session, *, tenant_id: int = 1
) -> dict[str, list[str]]:
    """Собрать pinned URLs из watchlist, сгруппированные по сайту."""
    out: dict[str, list[str]] = {site: [] for site in SCRAPER_CLASSES}
    for tp in watchlist.list_tracked(
        session, active_only=True, tenant_id=tenant_id
    ):
        for link in tp.links:
            if link.url and link.status == "confirmed" and link.site in out:
                out[link.site].append(link.url)
    return out


def auto_match_watchlist(session, *, tenant_id: int = 1) -> int:
    """Привязать Product'ы к Match-кластеру для каждого TrackedProduct.

    Логика: для каждой TrackedProduct → получить или создать Match (canonical_name,
    brand, etc.) → найти Product'ы по URL = TrackedProductLink.url и поставить им
    canonical_id. Это даёт мгновенный cross-site matching без эвристики.

    Возвращает кол-во привязанных Product'ов.
    """
    from sqlalchemy import select

    from src.product_policy import policy_identity_eligibility, policy_offer_eligibility

    matcher.acquire_match_mutation_xact_lock(session)
    linked = 0
    for tp in watchlist.list_tracked(
        session, active_only=True, tenant_id=tenant_id
    ):
        # Найти/создать Match для этого TrackedProduct
        match = session.scalar(
            select(storage.Match).where(
                storage.Match.canonical_name == tp.canonical_name,
                storage.Match.is_manual.is_(True),
                storage.Match.tenant_id == tenant_id,
            )
        )
        if not match:
            match = storage.Match(
                tenant_id=tenant_id,
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
                    storage.Product.tenant_id == tenant_id,
                )
            )
            if product and product.canonical_id != match.id:
                # Do not trust ``match.products`` here: this loop mutates
                # ``canonical_id`` directly and the already-loaded relationship
                # can stay stale until it is expired.  Query the current cohort
                # after an autoflush so every subsequent watchlist link is
                # checked against products linked earlier in this same call.
                cohort_members = list(
                    session.scalars(
                        select(storage.Product).where(
                            storage.Product.canonical_id == match.id,
                            storage.Product.tenant_id == tenant_id,
                            storage.Product.url_dead_at.is_(None),
                        )
                    ).all()
                )
                cohort = [
                    member
                    for member in cohort_members
                    if member.site != product.site and member.url_dead_at is None
                ] + [product]
                identity = policy_identity_eligibility(cohort)
                offer = policy_offer_eligibility(product)
                if not identity.eligible or not offer.eligible:
                    log.warning(
                        "watchlist_match_policy_rejected",
                        tracked_product_id=tp.id,
                        product_id=product.id,
                        identity_reason=identity.reason,
                        offer_reason=offer.reason,
                    )
                    continue
                product.canonical_id = match.id
                session.flush()
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


_RUN_BASELINE_MIN_FRACTION = 0.50
_RUN_QUALITY_MAX_ITEMS = 500


class RunQualityFailure(RuntimeError):
    """Scrape phase produced no trustworthy site result."""


def classify_run_quality(
    results: list[ScrapeResult],
    requested_sites: list[str],
    *,
    mode: str,
    baselines: dict[str, int | None] | None = None,
    enforce_baseline: bool = False,
) -> tuple[str, dict]:
    """Classify a completed scrape phase as ok/degraded/failed.

    The classifier is deliberately conservative only for full category runs.
    A category-id, limit, watchlist or intraday run is intentionally partial and
    therefore is not compared with a full-catalog baseline. Unit-level failures
    (category or URL) are still visible and degrade the run in every mode.
    """
    baselines = baselines or {}
    by_site = {result.site: result for result in results}
    site_details: dict[str, dict] = {}

    for site in requested_sites:
        result = by_site.get(site)
        if result is None:
            site_details[site] = {
                "status": "failed",
                "products": 0,
                "items_expected": 0,
                "items_completed": 0,
                "items_failed": 0,
                "baseline_products": None,
                "baseline_fraction": None,
                "reasons": ["missing_site_result"],
                "errors": ["scraper returned no result for requested site"],
                "errors_truncated": 0,
                "items": {},
                "items_truncated": 0,
            }
            continue

        expected = max(0, int(result.items_expected or 0))
        completed = max(0, int(result.items_completed or 0))
        failed = max(0, int(result.items_failed or 0))
        if expected == 0 and result.route_statuses:
            expected = len(result.route_statuses)
            completed = sum(
                1
                for route in result.route_statuses.values()
                if route.complete and route.pages_skipped == 0 and route.item_failures == 0
            )
            failed = expected - completed
        products = len(result.products)
        baseline = baselines.get(site)
        reasons: list[str] = []
        ordered_items = list(result.item_results.items())
        ordered_items.sort(key=lambda item: item[1].get("status") == "ok")
        route_total = len(result.route_statuses)
        incomplete_route_details: dict[str, dict] = {}
        for slug, status in list(result.route_statuses.items())[:_RUN_QUALITY_MAX_ITEMS]:
            reason = _route_incomplete_reason(status)
            if reason is None:
                continue
            incomplete_route_details[str(slug)[:300]] = {
                "reason": reason[:200],
                **({"item_failures": int(status.item_failures)} if status.item_failures else {}),
                **({"pages_skipped": int(status.pages_skipped)} if status.pages_skipped else {}),
                **(
                    {"raw_items": int(status.raw_items), "parsed_items": int(status.parsed_items)}
                    if status.raw_items or status.parsed_items
                    else {}
                ),
            }

        persisted_items: dict[str, dict] = {}
        for index, (raw_key, raw_value) in enumerate(ordered_items[:_RUN_QUALITY_MAX_ITEMS]):
            key = str(raw_key)[:300]
            if key in persisted_items:
                suffix = f"~{index}"
                key = f"{key[: 300 - len(suffix)]}{suffix}"
            value = raw_value if isinstance(raw_value, dict) else {}
            persisted_items[key] = {
                "status": str(value.get("status") or "unknown")[:30],
                "products": max(0, int(value.get("products") or 0)),
                **({"error": str(value.get("error"))[:500]} if value.get("error") else {}),
                **(
                    {"error_kind": str(value.get("error_kind"))[:50]}
                    if value.get("error_kind")
                    else {}
                ),
            }

        if result.site_fatal:
            status = "failed"
            reasons.append("site_fatal")
        elif expected == 0:
            status = "failed"
            reasons.append("no_items_requested")
        elif products == 0:
            status = "failed"
            reasons.append("zero_products")
        else:
            status = "ok"
            if failed > 0 or completed < expected:
                status = "degraded"
                reasons.append("incomplete_items")
            if result.errors:
                status = "degraded"
                reasons.append("scraper_errors")
            if (
                enforce_baseline
                and isinstance(baseline, int)
                and baseline > 0
                and products < baseline * _RUN_BASELINE_MIN_FRACTION
            ):
                status = "degraded"
                reasons.append("below_baseline")

        site_details[site] = {
            "status": status,
            "products": products,
            "items_expected": expected,
            "items_completed": completed,
            "items_failed": failed,
            "site_fatal": bool(result.site_fatal),
            "baseline_products": baseline if isinstance(baseline, int) and baseline > 0 else None,
            "baseline_fraction": (
                round(products / baseline, 4)
                if isinstance(baseline, int) and baseline > 0
                else None
            ),
            "reasons": reasons,
            "errors": [str(error)[:500] for error in result.errors[:20]],
            "errors_truncated": max(0, len(result.errors) - 20),
            "items": persisted_items,
            "items_truncated": max(0, len(ordered_items) - len(persisted_items)),
            # Route evidence is what full-catalog verification actually judges, and
            # it used to exist only in memory: `items_expected/completed` above are
            # counted from the requested slugs (base.py:748,830), NOT from
            # route_statuses, so a run could report 6/6 items completed while
            # verification failed all 6 routes — and nothing recorded why.
            # Only INCOMPLETE routes are persisted: a healthy 300-category run would
            # otherwise add ~63 KB of "complete: true" to every row, while a failing
            # run is exactly the one whose evidence must survive intact.
            "routes_incomplete": incomplete_route_details,
            "routes_complete_count": route_total - len(incomplete_route_details),
        }

    statuses = [row["status"] for row in site_details.values()]
    if not statuses or all(status == "failed" for status in statuses):
        overall = "failed"
    elif any(status != "ok" for status in statuses):
        overall = "degraded"
    else:
        overall = "ok"

    full_catalog_verified = overall == "ok" and mode == "category" and enforce_baseline
    return overall, {
        "version": 1,
        "mode": mode,
        "baseline_enforced": enforce_baseline,
        "baseline_min_fraction": _RUN_BASELINE_MIN_FRACTION if enforce_baseline else None,
        "full_catalog_verified": full_catalog_verified,
        "financially_eligible": full_catalog_verified,
        "sites": site_details,
    }


def scope_category_run_sites(
    slugs_by_site: dict[str, list[str]],
    requested_sites: list[str],
    *,
    category_id: int | None,
) -> tuple[list[str], dict[str, list[str]]]:
    """Limit a single-category run to sites where that category has a route.

    A category can intentionally exist on only one or two sites. Treating the
    other requested sites as ``no_items_requested`` makes a successful partial
    category scan look degraded. Full-catalog runs stay fail-closed: an empty
    site configuration is preserved so the quality classifier can reject it.
    If a single-category row has no route anywhere, preserve the original scope
    as well so the run fails instead of becoming a vacuous success.
    """
    if category_id is None:
        return list(requested_sites), slugs_by_site

    configured = {
        site: slugs_by_site.get(site, []) for site in requested_sites if slugs_by_site.get(site)
    }
    if not configured:
        return list(requested_sites), slugs_by_site
    return list(configured), configured


def is_run_financially_eligible(run: storage.Run | None) -> bool:
    return storage.run_is_financially_eligible(run)


def run_quality_message(status: str, quality: dict) -> str | None:
    if status == "ok":
        return None
    fragments: list[str] = []
    for site, details in (quality.get("sites") or {}).items():
        if details.get("status") == "ok":
            continue
        reasons = ",".join(details.get("reasons") or ["unknown"])
        fragments.append(f"{site}={details.get('status')}({reasons})")
    if not fragments:
        fragments.append("no requested scrape work produced a valid result")
    return f"run quality {status}: " + "; ".join(fragments)


def mark_scrape_request_terminal(
    session: Session,
    request_id: int,
    run: storage.Run,
) -> storage.ScrapeRequest | None:
    """Copy the authoritative scrape-phase terminal state to the UI queue."""
    request = session.get(storage.ScrapeRequest, request_id)
    if request is None or request.tenant_id != run.tenant_id:
        return None
    request.run_id = run.id
    request.status = run.status
    request.completed_at = utcnow()
    request.error_message = run.error_message
    session.commit()
    return request


def run_tenant_id_for_request(session: Session, request_id: int | None) -> int:
    """Resolve CLI run ownership from an active queue request."""
    if request_id is None:
        return 1
    request = session.get(storage.ScrapeRequest, request_id)
    if request is None:
        raise click.ClickException(f"scrape request #{request_id} not found")
    if request.status not in {"pending", "running"}:
        raise click.ClickException(f"scrape request #{request_id} is already {request.status}")
    if request.tenant_id != 1:
        raise click.ClickException(
            "Server-side scraping is not enabled for non-pilot tenants; request blocked."
        )
    return request.tenant_id


_FULL_CATALOG_MIN_BASELINE_FRACTION = 0.90


_ROUTE_CAUSE_MAX_LEN = 40
_MAX_CAUSE_KINDS_PER_SITE = 3


def _route_incomplete_causes(status) -> list[str]:
    """Every condition that makes this route fail verification; [] if it passes.

    Non-empty exactly when `_verify_full_catalog_results` would reject the route
    — the `fails` expression below is the gate, restated once. Everything after
    it only explains; it can never change the verdict.

    Reports ALL firing causes, not the first, and puts the scraper's own
    `abort_reason` first. Ordering by field position is actively wrong here:
    aloe sets `pages_skipped=total_card_failures` AND `item_failures=
    total_card_failures` (src/scrapers/aloe.py:616,627) — the same card-failure
    count in both — so reading `pages_skipped` first reports a parse failure as
    a pagination gap. Those two need opposite fixes (under-fetch → fix the
    scraper; parse failures at 99.9% coverage → this check is too strict), so a
    confident wrong label is worse than the blank we had.
    """
    if status is None:
        return ["missing_route_status"]

    count_mismatch = status.expected_items is not None and (
        status.raw_items != status.expected_items or status.parsed_items != status.expected_items
    )
    fails = (
        not status.complete
        or status.pages_skipped > 0
        or status.item_failures > 0
        or count_mismatch
    )
    if not fails:
        return []

    causes: list[str] = []
    if status.abort_reason:
        causes.append(str(status.abort_reason)[:_ROUTE_CAUSE_MAX_LEN])
    if status.item_failures > 0:
        causes.append("item_parse_failures")
    if status.pages_skipped > 0:
        causes.append("pages_skipped")
    if count_mismatch:
        causes.append("item_count_mismatch")
    return causes or ["incomplete"]


def _route_incomplete_reason(status) -> str | None:
    """Human-readable detail for one route, or None if it passes."""
    causes = _route_incomplete_causes(status)
    if not causes:
        return None
    detail = "+".join(causes)
    if status is not None and status.expected_pages is not None:
        detail += f"(pages={status.visited_pages}/{status.expected_pages})"
    return detail


def _verify_full_catalog_results(
    results: list[ScrapeResult],
    *,
    sites: list[str],
    expected_slugs: dict[str, list[str]],
    baselines: dict[str, int | None],
) -> tuple[bool, str]:
    """Verify an unbounded category scan with fail-closed route evidence."""
    from collections import Counter

    by_site = {result.site: result for result in results}
    missing_sites = [site for site in sites if site not in by_site]
    failed_sites: list[str] = []
    coverage_failures: list[str] = []
    zero_categories: dict[str, int] = {}
    incomplete_routes: dict[str, list[str]] = {}
    for site in sites:
        result = by_site.get(site)
        slugs = [str(slug) for slug in expected_slugs.get(site, []) if slug is not None]
        if result is None:
            continue
        if not slugs or not result.products or bool(result.errors):
            failed_sites.append(site)
        baseline = baselines.get(site) or 0
        if baseline > 0 and len(result.products) < baseline * _FULL_CATALOG_MIN_BASELINE_FRACTION:
            coverage_failures.append(site)
        zero_count = sum(
            1 for slug in slugs if int(result.category_counts.get(slug, 0)) == 0
        )
        if zero_count:
            zero_categories[site] = zero_count
        # Single source of truth for "is this route complete", so the reason we
        # report can never drift from the condition we fail on.
        # Single source of truth for "is this route complete", so the reason we
        # report can never drift from the condition we fail on.
        incomplete = [
            (slug, causes)
            for slug in slugs
            if (causes := _route_incomplete_causes(result.route_statuses.get(slug)))
        ]
        if incomplete:
            incomplete_routes[site] = incomplete

    verified = not (
        missing_sites
        or failed_sites
        or coverage_failures
        or zero_categories
        or incomplete_routes
    )
    if verified:
        return True, "complete_nonzero_routes_coverage_ok"
    zero_summary = ",".join(
        f"{site}:{count}" for site, count in sorted(zero_categories.items())
    )
    # Report WHY, not just how many — `aloe:6` told nobody anything. Aggregate by
    # cause KIND (no embedded counts, or every route would be its own bucket) and
    # keep only the top few per site: the column is varchar(300), and a blind
    # `reason[:300]` used to drop whole trailing SITES, losing information the
    # bare `aloe:40,zeytun:6` form had kept.
    def _site_summary(site: str, routes: list) -> str:
        kinds = Counter(cause for _slug, causes in routes for cause in causes)
        shown = kinds.most_common(_MAX_CAUSE_KINDS_PER_SITE)
        body = ",".join(f"{kind}x{count}" for kind, count in shown)
        if len(kinds) > len(shown):
            body += f",+{len(kinds) - len(shown)}more"
        return f"{site}:{len(routes)}[{body}]"

    incomplete_summary = ",".join(
        _site_summary(site, routes) for site, routes in sorted(incomplete_routes.items())
    )
    reason = (
        f"missing={','.join(missing_sites) or '-'};"
        f"failed={','.join(failed_sites) or '-'};"
        f"coverage={','.join(coverage_failures) or '-'};"
        f"zero_categories={zero_summary or '-'};"
        f"incomplete_routes={incomplete_summary or '-'}"
    )
    return False, reason[:300]


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
    from src.storage import AlertEvent, AlertRule, Product, Run
    from sqlalchemy import func, select, desc

    for result in results:
        site = result.site
        current = len(result.products) if hasattr(result, "products") else 0
        if current == 0:
            continue

        # P0.5 (PO Audit 2026-05-17): if current < 10% of known site catalog,
        # this is almost certainly a partial/cancelled scrape (GH Actions
        # timeout, user-cancelled workflow, network failure mid-run), NOT a
        # genuine "site dropped 93% of products". Раньше cancelled GH runs
        # давали 119/249 prods → smoke генерил false-positive «-93%» alerts
        # которые шумели в /alerts и Sentry.
        catalog_size = (
            session.scalar(select(func.count()).select_from(Product).where(Product.site == site))
            or 0
        )
        if catalog_size > 0 and current < 0.10 * catalog_size:
            log.info(
                "smoke_test_skipped_partial_run",
                site=site,
                current=current,
                catalog_size=catalog_size,
                reason="current<10%_of_catalog — likely cancelled/partial scrape",
            )
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
                site=site,
                current=current,
                avg=round(avg, 1),
                ratio=round(ratio, 2),
            )
            # Find or create site_drop rule
            rule = session.scalar(
                select(AlertRule).where(AlertRule.rule_type == "site_drop_smoke").limit(1)
            )
            if not rule:
                rule = AlertRule(
                    name="smoke_site_drop",
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
            session.add(
                AlertEvent(
                    rule_id=rule.id,
                    rule_type="site_drop_smoke",
                    dedup_key=f"site_drop_smoke|run={run.id}|site={site}",
                    severity="warning",
                    title=f"Site {site} собрал на {round((1 - ratio) * 100)}% меньше обычного",
                    detail=(
                        f"В этом прогоне site={site} собрал {current} товаров. "
                        f"Среднее за прошлые {len(prev_counts)} ok-runs: {round(avg)}. "
                        f"Возможно сменилась вёрстка или rate-limit."
                    ),
                    payload={"site": site, "current": current, "avg": round(avg, 1)},
                )
            )
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

    from src import brand_resolver
    from src.brand_catalog import extract_brand
    from src.normalize import extract_dosage, extract_pack_size, normalize_name
    from src.product_observations import apply_product_observation

    def _compute_brand_verified(sp) -> str | None:
        """Настоящий бренд из АВТОРИТЕТНОГО источника (см. brand_resolver):
        aloe — уже чистое поле brand; pharmonline — токен бренда из slug;
        aptekonline — None здесь (заполняется отдельным backfill через page-JSON,
        т.к. category-API бренд не отдаёт)."""
        if sp.site == "aloe":
            return sp.brand
        if sp.site == "pharmonline":
            return brand_resolver.brand_from_pharmonline_slug(sp.url)
        return None

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
                bverified = _compute_brand_verified(sp)
                # Дозировка/пакет: если скрейпер не вернул, извлечём из имени.
                dosage = sp.dosage or extract_dosage(sp.name)
                pack = sp.pack_size or extract_pack_size(sp.name)
                prepared.append((sp, normalized, brand, dosage, pack))

                existing = existing_by_key.get((sp.site, sp.external_id))
                if existing:
                    existing.name = sp.name
                    existing.name_normalized = normalized
                    existing.brand = brand or existing.brand
                    # brand_verified: pharmonline/aloe обновляем; aptek (None
                    # здесь) НЕ затираем backfill-значение через `or existing`.
                    existing.brand_verified = bverified or existing.brand_verified
                    existing.manufacturer = sp.manufacturer or existing.manufacturer
                    existing.dosage = dosage or existing.dosage
                    existing.pack_size = pack or existing.pack_size
                    existing.image_url = sp.image_url or existing.image_url
                    existing.category = sp.category or existing.category
                    # Регрессия 2026-05-27: existing.url не обновлялся → 6238
                    # pharmonline-продуктов застряли на '/True' после фикса
                    # `path` vs `postQuery` в pharmonline_ddp. Свежие прогоны
                    # с правильным slug должны перезаписать broken URL.
                    # Пустой/None от скрейпера — keep existing (defensive).
                    existing.url = sp.url or existing.url
                    # Phase 2.1 — barcode never overwrites existing non-null
                    # (some scrape runs may temporarily lack the field).
                    if sp.barcode and not existing.barcode:
                        existing.barcode = sp.barcode
                    existing.last_seen_at = utcnow()
                else:
                    product = storage.Product(
                        site=sp.site,
                        external_id=sp.external_id,
                        url=sp.url,
                        name=sp.name,
                        name_normalized=normalized,
                        brand=brand,
                        brand_verified=bverified,
                        manufacturer=sp.manufacturer,
                        category=sp.category,
                        dosage=dosage,
                        pack_size=pack,
                        image_url=sp.image_url,
                        description=sp.description,
                        barcode=sp.barcode,
                    )
                    new_products.append(product)
                    existing_by_key[(sp.site, sp.external_id)] = product

            # === Flush новых продуктов (нужен RETURNING id для FK) ===
            if new_products:
                session.add_all(new_products)
                session.flush()

            # Identity and website-offer history are independent from diff-only
            # price snapshots, so every explicit scrape observation is stored.
            observed_at = utcnow()
            observations: list[storage.OfferObservation] = []
            for sp, _, _, _, _ in prepared:
                product = existing_by_key[(sp.site, sp.external_id)]
                observations.append(
                    apply_product_observation(
                        product,
                        sp,
                        run_id=run.id,
                        observed_at=observed_at,
                    )
                )
            session.add_all(observations)

            # === Pre-fetch latest snapshots — для diff-only решения ===
            existing_product_ids = [p.id for p in existing_by_key.values() if p.id is not None]
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


@cli.command("db-check")
@click.option("--fix", is_flag=True, help="Автоматически чинить orphans (удалять)")
def db_check_cmd(fix: bool) -> None:
    """Проверить целостность БД: orphan matches, NULL prices, висящие references.

    Запуск:
        pharmacy-monitor db-check          # только репорт
        pharmacy-monitor db-check --fix    # удалить orphan'ов

    Exit-code: 0 = OK, 1 = найдены проблемы (без --fix), 2 = ошибка SQL
    """
    from sqlalchemy import select as _s, func as _f, text

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
        orphan_matches = s.scalars(_s(storage.Match).where(~storage.Match.products.any())).all()
        if orphan_matches:
            issues_found += len(orphan_matches)
            click.echo(f"⚠️  Orphan matches (без products): {len(orphan_matches)}")
            if fix:
                for m in orphan_matches:
                    s.delete(m)
                s.commit()
                click.echo(f"   → Удалено {len(orphan_matches)}")

        # 3. Snapshots с product_id ссылающимися на удалённый Product
        stale_snaps = s.scalar(
            text("""
            SELECT COUNT(*) FROM price_snapshots ps
            LEFT JOIN products p ON p.id = ps.product_id
            WHERE p.id IS NULL
        """)
        )
        if stale_snaps:
            issues_found += stale_snaps
            click.echo(f"⚠️  Snapshots с битой product_id: {stale_snaps}")
            if fix:
                s.execute(
                    text("""
                    DELETE FROM price_snapshots
                    WHERE product_id NOT IN (SELECT id FROM products)
                """)
                )
                s.commit()
                click.echo(f"   → Удалено {stale_snaps}")

        # 4. Products с canonical_id указывающим на удалённый Match
        stale_canon = s.scalar(
            text("""
            SELECT COUNT(*) FROM products p
            LEFT JOIN matches m ON m.id = p.canonical_id
            WHERE p.canonical_id IS NOT NULL AND m.id IS NULL
        """)
        )
        if stale_canon:
            issues_found += stale_canon
            click.echo(f"⚠️  Products с битой canonical_id: {stale_canon}")
            if fix:
                s.execute(
                    text("""
                    UPDATE products SET canonical_id = NULL
                    WHERE canonical_id NOT IN (SELECT id FROM matches)
                """)
                )
                s.commit()
                click.echo(f"   → Обнулено canonical_id в {stale_canon}")

        # 5. NULL prices (более 80% snapshots без price — подозрительно)
        total_snaps = s.scalar(_s(_f.count(storage.PriceSnapshot.id))) or 0
        null_prices = (
            s.scalar(
                _s(_f.count(storage.PriceSnapshot.id)).where(storage.PriceSnapshot.price.is_(None))
            )
            or 0
        )
        if total_snaps > 0:
            null_pct = null_prices / total_snaps * 100
            if null_pct > 80:
                issues_found += null_prices
                click.echo(f"⚠️  NULL prices: {null_prices}/{total_snaps} ({null_pct:.0f}%)")
            else:
                click.echo(f"✓ NULL prices в норме: {null_prices}/{total_snaps} ({null_pct:.1f}%)")

        # 6. Дубликаты Products (same site + same external_id) — должно быть 0 за счёт UniqueConstraint
        dup_products = count_duplicate_products(s)
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


@cli.command("reap-stale-runs")
@click.option(
    "--max-age-hours",
    type=float,
    default=6.0,
    help="Mark unfinished runs older than N hours as failed.",
)
@click.option(
    "--reason",
    default="reaped stale unfinished run after interrupted/timeout process",
    help="Reason appended to run.error_message.",
)
def reap_stale_runs_cmd(max_age_hours: float, reason: str) -> None:
    """Mark orphaned runs without ``finished_at`` as failed.

    Intended for server watcher use after it confirms no scrape/rematch process
    is active. Does not kill processes.
    """
    storage.init_db()
    Session = storage.make_session()
    if not _hold_scrape_lock_until_command_exit(Session, wait=False):
        raise click.ClickException(
            "recovery refused: an active scrape/rematch producer holds the run lock"
        )
    with Session() as s:
        count = reap_stale_running_runs(s, max_age_hours=max_age_hours, reason=reason)
    click.echo(f"reaped {count} stale running run(s)")


def _read_health_alert_state(path: str) -> dict | None:
    """Прочитать состояние последнего отправленного health-алерта (для дедупа)."""
    import json
    from pathlib import Path

    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write_health_alert_state(path: str, signature: str) -> None:
    """Записать подпись + время последнего отправленного алерта. Сбой записи не
    фатален — деградируем до «слать всегда» (безопасно)."""
    import json
    from pathlib import Path

    from src._time import utcnow

    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"signature": signature, "sent_at": utcnow().isoformat()}))
    except OSError as e:
        log.warning("health_alert_state_write_failed", path=path, error=str(e))


@cli.command("health-check")
@click.option(
    "--max-age-hours",
    type=int,
    default=26,
    help="Алерт если последний прогон старше N часов (по умолчанию 26 — суточный cron + jitter)",
)
@click.option(
    "--min-products",
    type=int,
    default=1,
    help="Алерт если последний прогон собрал меньше N товаров",
)
@click.option(
    "--alert-email",
    is_flag=True,
    help="Отправить email-алерт получателям при warning/critical",
)
@click.option(
    "--quiet-on-ok",
    is_flag=True,
    help="Без вывода если статус ok (для cron — пишет только при проблемах)",
)
@click.option(
    "--alert-cooldown-hours",
    type=float,
    default=6.0,
    help="Анти-спам: не слать ТОТ ЖЕ набор проблем чаще раза в N часов (деф. 6)",
)
@click.option(
    "--alert-state-file",
    envvar="HEALTH_ALERT_STATE_FILE",
    default="data/health_alert_state.json",
    help="Файл состояния для дедупа алертов (env HEALTH_ALERT_STATE_FILE)",
)
def health_check_cmd(
    max_age_hours: int,
    min_products: int,
    alert_email: bool,
    quiet_on_ok: bool,
    alert_cooldown_hours: float,
    alert_state_file: str,
) -> None:
    """Проверить здоровье системы: stale/failed/empty/site-drop. Exit-code 0=ok, 1=warning, 2=critical."""
    from src.health import check_health, render_alert_html

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        report = check_health(s, max_age_hours=max_age_hours, min_products=min_products)

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
        from src._time import utcnow
        from src.health import alert_due, alert_signature

        sig = alert_signature(report)
        state = _read_health_alert_state(alert_state_file)
        if alert_due(sig, state, now=utcnow(), cooldown_hours=alert_cooldown_hours):
            try:
                html = render_alert_html(report)
                notifier.send_email(
                    subject=f"Pharmacy Monitor — {report.status.upper()}",
                    html_body=html,
                )
                _write_health_alert_state(alert_state_file, sig)
                click.echo("→ Email-алерт отправлен")
            except Exception as e:
                click.echo(f"⚠️ Не удалось отправить email-алерт: {e}", err=True)
        else:
            click.echo(
                f"→ Email подавлен (cooldown {alert_cooldown_hours}ч — "
                f"те же проблемы уже отправлены)"
            )

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
        f"OK: total={result.total} matched={result.matched_to_product} unmatched={result.unmatched}"
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
        f"OK: total={result.total} matched={result.matched_to_product} unmatched={result.unmatched}"
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
            click.echo("(в продакшене — отправить ссылкой в email)")
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
    "--site",
    type=click.Choice(["pharmonline", "aptekonline", "aloe"]),
    default=None,
    help="Ограничить правило одним сайтом",
)
@click.option(
    "--channels",
    default="email",
    help="Список каналов через запятую: email,telegram",
)
@click.option("--cooldown-hours", type=int, default=12)
def alert_add_rule(
    rule_type: str,
    name: str | None,
    min_pct: float | None,
    site: str | None,
    channels: str,
    cooldown_hours: int,
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
    "--dispatch",
    is_flag=True,
    help="Не только посчитать, но и отправить по каналам (email/telegram)",
)
@click.option(
    "--rule-id",
    type=int,
    multiple=True,
    help="Прогнать только указанные правила (можно несколько)",
)
def alert_evaluate(dispatch: bool, rule_id: tuple[int, ...]) -> None:
    """Прогнать движок алертов вручную."""
    from src import alerts as alerts_mod

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        fired = alerts_mod.evaluate_rules(s, rule_ids=list(rule_id) if rule_id else None)
        click.echo(f"Сработало: {len(fired)}")
        for ev in fired:
            click.echo(f"  [{ev.severity}] {ev.rule_type}: {ev.title[:80]}")
        if dispatch and fired:
            from src import notifications as notif_mod

            results = notif_mod.dispatch_events_batch(s, fired)
            click.echo(f"  → отправлено одним письмом: {results}")


@alert_group.command("recent")
@click.option("--limit", type=int, default=20)
def alert_recent(limit: int) -> None:
    """Последние сработавшие events."""
    from sqlalchemy import desc, select as sa_select

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        events = s.scalars(
            sa_select(storage.AlertEvent).order_by(desc(storage.AlertEvent.created_at)).limit(limit)
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
    "--once",
    is_flag=True,
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
    "--timeout",
    type=int,
    default=30,
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


# ─── Standalone digest command ───────────────────────────────────────────────


@cli.command("digest")
@click.option(
    "--top", "top_n", type=int, default=20, show_default=True, help="Топ-N алертов в письме."
)
@click.option(
    "--window", "window_hours", type=int, default=24, show_default=True, help="Окно выборки, часов."
)
@click.option("--dry-run", is_flag=True, help="Вывести preview в stdout, не отправлять.")
def digest_cmd(top_n: int, window_hours: int, dry_run: bool) -> None:
    """Отправить daily digest (топ-N алертов за последние window_hours часов).

    Запускается systemd timer pharmacy-monitor-digest@daily.timer в 05:00 UTC
    (09:00 Baku). При 0 событий — письмо не отправляется.
    """
    from src.digest import send_daily_digest

    storage.init_db()
    Session = storage.make_session()
    with Session() as s:
        n = send_daily_digest(s, window_hours=window_hours, top_n=top_n, dry_run=dry_run)
        if n:
            click.echo(f"OK: digest sent, {n} events")
        else:
            click.echo("Skipped: no events in window" if not dry_run else f"[dry-run] {n} events")


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


@notify_group.command("test")
@click.option(
    "--email", "email_to", default=None, help="Override-получатель (default: DB/EMAIL_TO)"
)
@click.option(
    "--chat-id", "chat_id", default=None, help="Telegram chat_id (default: TELEGRAM_CHAT_ID)"
)
def notify_test(email_to: str | None, chat_id: str | None) -> None:
    """Smoke-тест доставки алертов: шлёт тест-письмо + Telegram, репортит каждый канал.

    Запускать ПОСЛЕ configure-integrations.sh для проверки ключей. Неконфигурированные
    каналы помечаются 'skipped' (не ошибка). Пример (на проде):
        pharmacy-monitor notify test
    """
    import os

    from src import notifier

    # ── Email ──
    if os.environ.get("SMTP_HOST"):
        try:
            notifier.send_email(
                subject="🧪 Pharmacy Monitor — тест доставки",
                html_body="<p>Если вы это видите — SMTP настроен корректно.</p>",
                to=[email_to] if email_to else None,
            )
            click.echo("email:    OK (отправлено)")
        except Exception as e:
            click.echo(f"email:    FAIL — {e}")
    else:
        click.echo("email:    skipped (SMTP_HOST не задан)")

    # ── Telegram ──
    cid = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        click.echo("telegram: skipped (TELEGRAM_BOT_TOKEN не задан)")
    elif not cid:
        click.echo("telegram: skipped (нет chat_id — задай TELEGRAM_CHAT_ID или --chat-id)")
    else:
        ok = notifier.send_telegram_message(
            cid, "🧪 Pharmacy Monitor — тест доставки. Если видите это — Telegram настроен."
        )
        click.echo("telegram: OK" if ok else "telegram: FAIL — проверь токен/chat_id")


@cli.command("ai-crawl")
@click.option(
    "--site",
    type=click.Choice(["aloe", "pharmonline", "aptekonline"]),
    required=True,
    help="Сайт для AI-обхода (sitemap + LLM extraction)",
)
@click.option(
    "--max-urls",
    type=int,
    default=200,
    help="Макс. URL за сессию (default 200; на больших сайтах >1000 = ~$1-3)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Только discover URLs + heuristic-классификация, без LLM-extract (бесплатно).",
)
@click.option(
    "--budget-usd",
    type=float,
    default=None,
    help="Override env AI_CRAWL_BUDGET_USD. Crawl аборт когда лимит превышен.",
)
def ai_crawl_cmd(
    site: str,
    max_urls: int,
    dry_run: bool,
    budget_usd: float | None,
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

    if dry_run:
        try:
            result = asyncio.run(_run())
        except SiteScrapeFatalError as exc:
            raise click.ClickException(site_fatal_error_message(exc))
        click.echo(
            f"[dry-run] AI-crawl {site}: errors={len(result.errors)}. "
            "См. structlog 'ai_crawl_summary' (logs/app.jsonl) для visited/captcha/cost."
        )
        return

    Session = storage.make_session()
    if not _hold_scrape_lock_until_command_exit(Session, wait=False):
        raise click.ClickException("AI crawl blocked because another scrape run is active")
    with Session() as session:
        run = storage.Run(
            status="running",
            catalog_scope="partial",
            catalog_verified=False,
            catalog_verification_reason="ai_crawl_not_full_catalog",
        )
        session.add(run)
        session.commit()
        try:
            try:
                result = asyncio.run(_run())
            except SiteScrapeFatalError as exc:
                result = site_fatal_result(site, ["ai_crawl"], exc)
            if result.items_expected == 0:
                result.items_expected = 1
                result.items_completed = 1 if result.products else 0
                result.items_failed = 1 if result.errors or not result.products else 0
                result.item_results = {
                    "ai_crawl": {
                        "status": (
                            "failed"
                            if not result.products
                            else "degraded"
                            if result.errors
                            else "ok"
                        ),
                        "products": len(result.products),
                        "error": "; ".join(result.errors[:5])[:500] or None,
                    }
                }
            count = persist_results(session, run, [result])
            run.products_scraped = count
            run.products_per_site = {site: len(result.products)}
            run.products_per_site_category = {site: _per_category_breakdown([result])}
            run.sites_completed = site
            quality_status, quality = classify_run_quality(
                [result],
                [site],
                mode="ai_crawl",
            )
            run.status = quality_status
            run.run_quality = quality
            run.error_message = run_quality_message(quality_status, quality)
            run.finished_at = utcnow()
            session.commit()
            if quality_status == "failed":
                raise RunQualityFailure(run.error_message or "run quality failed")
            click.echo(
                f"{quality_status}: AI-crawl {site} → run #{run.id}, "
                f"persisted {count} (из {len(result.products)} extracted), "
                f"errors={len(result.errors)}"
            )
        except RunQualityFailure as e:
            raise click.ClickException(str(e))
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
@click.option(
    "--dry-run", is_flag=True, help="Не отправлять email, только сгенерировать отчёт в reports/"
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Ограничить N товаров на категорию (только в category-режиме)",
)
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
    "--hourly",
    is_flag=True,
    help="Ежечасный микро-прогон: только watchlist + только pinned URL'ы. Быстрый.",
)
@click.option(
    "--no-alerts",
    is_flag=True,
    help="Пропустить evaluation алертов после прогона (по умолчанию запускается)",
)
@click.option(
    "--request-id",
    type=int,
    default=None,
    help="ID строки в scrape_requests. Если задан — после persist (но ДО matcher) "
    "немедленно проставляем честный status ok/degraded/failed + run_id, чтобы UI "
    "не ждал медленных matcher/analyzer фаз.",
)
def run_cmd(
    dry_run: bool,
    limit: int | None,
    site: tuple[str, ...],
    mode: str,
    category_id: int | None,
    hourly: bool,
    no_alerts: bool,
    request_id: int | None,
) -> None:
    """Полный прогон: scrape → match → analyze → report."""
    storage.init_db()
    sites = list(site) if site else list(SCRAPER_CLASSES.keys())

    Session = storage.make_session()
    # Full/manual runs wait for a short partial producer to finish. Conversely,
    # scrape-only/intraday producers below fail-fast while this lock is held.
    _hold_scrape_lock_until_command_exit(Session, wait=True)
    with Session() as session:
        maybe_seed_categories(session)

        run_tenant_id = run_tenant_id_for_request(session, request_id)
        run = storage.Run(status="running", tenant_id=run_tenant_id)
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

        is_full_catalog = (
            effective_mode == "category" and category_id is None and limit is None
        )
        run.catalog_scope = "full" if is_full_catalog else "partial"
        run.full_catalog_sites = ",".join(sites) if is_full_catalog else None
        run.catalog_verified = False
        run.catalog_verification_reason = (
            "pending" if is_full_catalog else "bounded_or_watchlist_run"
        )
        session.commit()

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

        trust_context = None
        try:
            quality_sites: list[str] = list(sites)
            quality_baselines: dict[str, int | None] = {}
            enforce_quality_baseline = False
            if effective_mode == "watchlist":
                filtered = {s: urls for s, urls in watchlist_urls.items() if s in sites and urls}
                quality_sites = list(filtered)
                results = asyncio.run(scrape_watchlist_all(filtered))
            else:
                # Категории из БД, опционально фильтр по одной category_id
                slugs_by_site = {
                    s: watchlist.categories_for_site(session, s, only_category_id=category_id)
                    for s in sites
                }
                quality_sites, slugs_by_site = scope_category_run_sites(
                    slugs_by_site,
                    sites,
                    category_id=category_id,
                )
                baselines = baselines_for_sites(session, sites)
                quality_baselines = run_quality_baselines_for_sites(
                    session,
                    sites,
                    tenant_id=run.tenant_id,
                )
                enforce_quality_baseline = limit is None and category_id is None

                # Инкрементальный persist: сохраняем каждую категорию СРАЗУ (callback
                # → persist_results коммитит per-result), чтобы медленный/оборванный/
                # зависший прогон (pharmonline DDP ~7ч) не терял уже собранное — без
                # него persist шёл только в конце = всё-или-ничего. Callback синхронный
                # → сериализуется на однопоточном event loop, общая session безопасна.
                # Финальный persist ниже остаётся (промо + count); повтор товаров
                # безвреден (diff-only: те же товары → 0 новых snapshot).
                def _persist_category(site_name, slug, cat_products):
                    persist_results(
                        session,
                        run,
                        [ScrapeResult(site=site_name, products=list(cat_products))],
                    )

                results = asyncio.run(
                    scrape_all(
                        slugs_by_site,
                        limit,
                        ai_fallback_baselines=baselines,
                        on_category=_persist_category,
                        aloe_country_map=load_aloe_country_map(session),
                    )
                )
                persist_aloe_country_mappings(session, results)
            count = persist_results(session, run, results)
            run.products_scraped = count
            run.products_per_site = {r.site: len(r.products) for r in results}
            run.products_per_site_category = {r.site: _per_category_breakdown([r]) for r in results}
            run.sites_completed = ",".join(r.site for r in results)
            quality_status, quality = classify_run_quality(
                results,
                quality_sites,
                mode=effective_mode,
                baselines=quality_baselines,
                enforce_baseline=enforce_quality_baseline,
            )
            if is_full_catalog:
                run.catalog_verified, run.catalog_verification_reason = (
                    _verify_full_catalog_results(
                        results,
                        sites=sites,
                        expected_slugs=slugs_by_site,
                        baselines=baselines,
                    )
                )
                financial_ok = quality_status == "ok" and run.catalog_verified
                quality["full_catalog_verified"] = financial_ok
                quality["financially_eligible"] = financial_ok
                quality["catalog_verification_reason"] = run.catalog_verification_reason
            run.run_quality = quality
            run.error_message = run_quality_message(quality_status, quality)
            if is_full_catalog and quality_status == "ok" and run.catalog_verified:
                run.status = "running"
            else:
                run.status = quality_status
            session.commit()

            # === Early-complete для UI-triggered bounded scrape (job queue) ===
            # Full-catalog runs stay running until matcher/alerts/analyzer/ROI
            # finish, because publishing ok earlier would expose untrusted money
            # output. Bounded/watchlist runs can surface scrape quality now.
            if request_id is not None and not is_full_catalog:
                req = mark_scrape_request_terminal(session, request_id, run)
                if req is not None:
                    log.info(
                        "scrape_request_marked_complete_early",
                        request_id=request_id,
                        run_id=run.id,
                        status=quality_status,
                        products_scraped=count,
                    )

            # A requested full scan that lost a page or even one source item is
            # not a successful producer.  Stop before matcher, alerts, reports,
            # or ROI publication; the exception handler records ``degraded``
            # (distinct from a crash) and fails the queue request explicitly.
            if is_full_catalog and not run.catalog_verified:
                raise FullCatalogVerificationError(
                    "full catalog verification failed: "
                    f"{run.catalog_verification_reason or 'unknown reason'}"
                )

            if quality_status == "failed":
                run.finished_at = utcnow()
                session.commit()
                log.error(
                    "run_quality_failed",
                    run_id=run.id,
                    quality=quality,
                )
                raise RunQualityFailure(run.error_message or "run quality failed")

            if quality_status == "degraded":
                log.warning(
                    "run_quality_degraded",
                    run_id=run.id,
                    quality=quality,
                )

            # === Smoke-test: per-site coverage drop ===
            # Если конкретный сайт собрал <50% от среднего за последние 5 ok-runs —
            # пишем warning в alert_events. Защищает от silent regressions.
            try:
                _smoke_test_per_site_coverage(session, results, run)
            except Exception as e:
                log.warning("smoke_test_failed", error=str(e))

            lock_taken = False
            try:
                log.info("matcher_lock_wait", run_id=run.id)
                _acquire_matcher_lock(session, wait=True)
                lock_taken = True
                if effective_mode == "watchlist":
                    linked = auto_match_watchlist(session)
                    log.info("watchlist_auto_matched", linked=linked)
                matcher.match_products(session)
                # Auto-revalidate: match_products линкует широко (bucket+fuzzy) и НЕ
                # блокирует guard-конфликты в primary-проходе → бренд/состав/вариант/сила
                # несоответствия пересоздаются каждый прогон. Чистим их сразу когерентным
                # split'ом (корень «whack-a-mole» — раньше требовался ручной rematch).
                try:
                    split_actions = matcher.revalidate_split(session)
                    if split_actions:
                        log.info("revalidate_split", clusters=len(split_actions))
                except Exception as _re:
                    log.error("revalidate_split_failed", error=str(_re))
                    raise RuntimeError(
                        f"identity revalidation failed: {type(_re).__name__}: {_re}"
                    ) from _re
                try:
                    flagged = matcher.flag_suspected_mismatches(session)
                    if flagged:
                        log.info("price_mismatch_flags_updated", changed=flagged)
                except Exception as _fe:
                    log.warning("flag_mismatches_failed", error=str(_fe))
            finally:
                if lock_taken:
                    _release_matcher_lock(session)

            # Internal consumers must calculate against this exact verified
            # full Run before it is published as ``ok``.  External API calls
            # do not inherit this context and therefore remain fail-closed.
            if is_full_catalog and run.catalog_verified:
                from src.product_policy import finalizing_trusted_run

                trust_context = finalizing_trusted_run(run.id)
                trust_context.__enter__()

            # === Real-time alerts ===
            if not no_alerts and is_run_financially_eligible(run):
                from src import alerts as alerts_mod, notifications as notif_mod

                fired = alerts_mod.evaluate_rules(session, run.id)
                if fired and not dry_run:
                    # Одно письмо-сводка на прогон (вместо письма на событие) —
                    # переоценка целой линейки больше не топит инбокс.
                    try:
                        notif_mod.dispatch_events_batch(session, fired)
                    except Exception as e:
                        log.warning("alert_dispatch_failed", error=str(e))
                    log.info("alerts_dispatched", count=len(fired))
                elif fired:
                    log.info("alerts_dispatch_skipped_dry_run", count=len(fired))
            elif not no_alerts:
                log.warning(
                    "alerts_skipped_run_quality",
                    run_id=run.id,
                    status=run.status,
                )
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
            # SCRAPE_REPORT_EMAIL=0 отключает его глобально (см. _report_email_enabled).
            report_inputs_ready = False
            if is_run_financially_eligible(run):
                from src import roi as roi_mod

                report_inputs_ready = roi_mod.financial_inputs_are_fresh(
                    session,
                    tenant_id=run.tenant_id,
                )
            if (
                report_inputs_ready
                and run.tenant_id == 1
                and not dry_run
                and not hourly
                and _report_email_enabled()
            ):
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
            elif not dry_run and not hourly and _report_email_enabled():
                log.warning(
                    "scrape_report_email_skipped_unverified_inputs",
                    run_id=run.id,
                    status=run.status,
                )

            # P0.1 (PO Audit 2026-05-17): pre-compute ROI actions для всех 3
            # сайтов и сохранить в roi_actions_cache. HTTP-handler
            # /dash/roi/actions читает оттуда → <50мс latency вместо
            # 15-30с inline compute (timeout'ило с 408 на 4 экранах).
            # When all three sites form a trusted epoch, every cache slice must
            # refresh successfully before the current run is published.  If a
            # different site's latest full attempt already closed that global
            # gate, cache publication is deferred while this producer's own
            # verified status remains truthful.  Partial/watchlist runs never
            # publish a catalog epoch and therefore do not rewrite this cache.
            if is_full_catalog and run.catalog_verified:
                from src import roi as _roi

                if _roi.financial_inputs_are_fresh(
                    session,
                    tenant_id=run.tenant_id,
                ):
                    summary = _roi.refresh_all_cached_actions(
                        session,
                        run_id=run_id,
                        tenant_id=run.tenant_id,
                    )
                    log.info("roi_cache_refreshed", run_id=run_id, **summary)
                    failed_sites = [site for site, value in summary.items() if value < 0]
                    if failed_sites:
                        raise RuntimeError(
                            "ROI refresh failed for trusted epoch: "
                            + ",".join(sorted(failed_sites))
                        )
                else:
                    # A verified per-site producer remains successful even when
                    # another site's latest full attempt is degraded or stale.
                    # ROI reads independently fail closed on the same freshness
                    # guard; a later verified site run will refresh the cache
                    # once the complete three-site epoch is trustworthy again.
                    log.warning(
                        "roi_cache_refresh_deferred_unverified_inputs",
                        run_id=run_id,
                        tenant_id=run.tenant_id,
                    )
            else:
                log.warning(
                    "roi_cache_refresh_skipped_run_quality",
                    run_id=run_id,
                    status=run.status,
                )

            if trust_context is not None:
                trust_context.__exit__(None, None, None)
                trust_context = None

            if is_full_catalog and run.catalog_verified:
                run.status = "ok"
            run.finished_at = utcnow()
            if request_id is not None:
                req = session.get(storage.ScrapeRequest, request_id)
                if req is not None:
                    req.run_id = run.id
                    req.status = run.status
                    req.completed_at = utcnow()
            session.commit()
            log.info(
                "run_finished",
                run_id=run_id,
                status=run.status,
                products=count,
            )
        except Exception as e:
            if trust_context is not None:
                trust_context.__exit__(*sys.exc_info())
                trust_context = None
            run.status = (
                "degraded" if isinstance(e, FullCatalogVerificationError) else "failed"
            )
            run.error_message = f"{type(e).__name__}: {e}"
            run.finished_at = utcnow()
            if request_id is not None:
                req = session.get(storage.ScrapeRequest, request_id)
                if req is not None:
                    req.run_id = run.id
                    req.status = run.status
                    req.completed_at = utcnow()
            session.commit()
            log.exception("run_failed", run_id=run_id)
            raise click.ClickException(str(e))


@cli.command("scrape")
@click.option("--limit", type=int, default=None)
@click.option("--site", multiple=True, type=click.Choice(list(SCRAPER_CLASSES.keys())))
@click.option(
    "--category-id",
    type=int,
    default=None,
    help="Скрейпить ТОЛЬКО эту категорию (по id из таблицы categories).",
)
def scrape_cmd(limit: int | None, site: tuple[str, ...], category_id: int | None) -> None:
    """Только скрейпинг — без анализа и отправки."""
    storage.init_db()
    sites = list(site) if site else list(SCRAPER_CLASSES.keys())

    Session = storage.make_session()
    if not _hold_scrape_lock_until_command_exit(Session, wait=False):
        click.echo("scrape: skipped because another scrape run is active")
        return
    with Session() as session:
        maybe_seed_categories(session)
        # Diagnostic producer only.  It intentionally cannot publish a trust
        # epoch because it does not run match revalidation, alerts or ROI
        # finalization.  ``run`` is the sole full-catalog publisher.
        run = storage.Run(
            status="running",
            catalog_scope="partial",
            full_catalog_sites=None,
            catalog_verified=False,
            catalog_verification_reason="scrape_command_diagnostic_non_publishing",
        )
        session.add(run)
        session.commit()
        try:
            slugs_by_site = {
                s: watchlist.categories_for_site(session, s, only_category_id=category_id)
                for s in sites
            }
            quality_sites, slugs_by_site = scope_category_run_sites(
                slugs_by_site,
                sites,
                category_id=category_id,
            )
            baselines = baselines_for_sites(session, sites)
            quality_baselines = run_quality_baselines_for_sites(
                session,
                sites,
                tenant_id=run.tenant_id,
            )
            results = asyncio.run(
                scrape_all(
                    slugs_by_site,
                    limit,
                    ai_fallback_baselines=baselines,
                    aloe_country_map=load_aloe_country_map(session),
                )
            )
            persist_aloe_country_mappings(session, results)
            count = persist_results(session, run, results)
            run.products_scraped = count
            run.products_per_site = {r.site: len(r.products) for r in results}
            run.products_per_site_category = {r.site: _per_category_breakdown([r]) for r in results}
            run.sites_completed = ",".join(r.site for r in results)
            quality_status, quality = classify_run_quality(
                results,
                quality_sites,
                mode="category",
                baselines=quality_baselines,
                enforce_baseline=limit is None and category_id is None,
            )
            quality["full_catalog_verified"] = False
            quality["financially_eligible"] = False
            quality["catalog_verification_reason"] = (
                "scrape_command_diagnostic_non_publishing"
            )
            run.status = quality_status
            run.run_quality = quality
            run.error_message = run_quality_message(quality_status, quality)
            run.finished_at = utcnow()
            session.commit()
            if quality_status == "failed":
                raise RunQualityFailure(run.error_message or "run quality failed")
            click.echo(f"Scraped {count} products in run #{run.id} ({quality_status})")
        except RunQualityFailure as e:
            raise click.ClickException(str(e))
        except Exception as e:
            run.status = "failed"
            run.error_message = str(e)
            run.finished_at = utcnow()
            session.commit()
            raise click.ClickException(str(e))


def _intraday_product_limit() -> int:
    """Bound an hourly point scan; full-catalog producers remain unlimited."""
    try:
        configured = int(os.environ.get("INTRADAY_PRODUCT_LIMIT", "600"))
    except ValueError:
        configured = 600
    return max(1, min(configured, 600))


@cli.command("intraday-tick")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Только показать (site, category), которые бы взяли — без скрейпа.",
)
def intraday_tick_cmd(dry_run: bool) -> None:
    """Phase 5.1 (Вариант C) — supplemental hourly scrape of ONE category on ONE site.

    Каждый вызов:
      1. Берёт top-30 volatile категорий (по count(price_snapshots) за 7 дней)
      2. Через Redis-rotation index выбирает следующую категорию
      3. Round-robin'ит сайт (pharmonline → aloe) с per-site rate-limit 2ч
      4. Запускает scrape-only persist для этой категории.

    Запускается из systemd timer ежечасно во время business hours (05-17 UTC).
    No-alerts чтобы не дублировать notifications с full nightly run.

    Skip-conditions (silent no-op, exit 0):
      - Redis недоступен → можем работать без rate-limit, продолжаем
      - 0 volatile categories (новый деплой, мало данных) → skip
      - Все sites locked (последний intraday на каждом < 2ч назад) → skip

    Failures (exit 1):
      - Сам scrape упал (network, proxy и т.п.) — поднимаем error чтобы systemd
        пометил unit failed и алерт сработал.
    """
    from src import intraday

    storage.init_db()
    Session = storage.make_session()

    with Session() as session:
        # dry_run → preview mode (без INCR rotation idx и без SETNX lock'а).
        target = intraday.pick_next_scrape_target(session, commit_state=not dry_run)
        if target is None:
            click.echo("intraday-tick: skipped (no volatile categories or all sites locked)")
            return

        site, cat = target
        product_limit = _intraday_product_limit()
        click.echo(
            f"intraday-tick: site={site} category_id={cat.id} key={cat.key} "
            f"label={cat.label_ru!r} limit={product_limit}"
        )

        if dry_run:
            click.echo("--dry-run: skipping actual scrape (no state mutation)")
            return

    # Run в новой сессии — отдельная transaction. Intraday должен только
    # обновить цены/наличие выбранной категории; полный matcher/analyzer/report
    # остаётся за nightly/manual full run, иначе 20-минутный systemd timeout
    # убивает тик посреди matcher и оставляет Run в status='running'.
    ctx = click.get_current_context()
    ctx.invoke(
        scrape_cmd,
        limit=product_limit,
        site=(site,),
        category_id=cat.id,
    )


@cli.command("rematch")
@click.option(
    "--reset",
    is_flag=True,
    default=False,
    help="Очистить все авто-матчи (canonical_id) перед пересчётом",
)
@click.option(
    "--threshold",
    type=int,
    default=None,
    help=f"Порог fuzzy (по умолчанию {matcher.FUZZY_THRESHOLD})",
)
@click.option(
    "--revalidate",
    is_flag=True,
    default=False,
    help="Точечно разбить существующие кластеры с конфликтом по текущим guard'ам",
)
@click.option(
    "--relink-dead",
    is_flag=True,
    default=False,
    help="Подменить мёртвые (url_dead_at) члены кластеров живой альтернативой того же спека",
)
@click.option(
    "--dry-run", is_flag=True, default=False, help="С --revalidate/--relink-dead: только показать"
)
def rematch_cmd(
    reset: bool, threshold: int | None, revalidate: bool, relink_dead: bool, dry_run: bool
) -> None:
    """Перезапустить матчинг (без скрейпинга). Полезно после изменения нормализации.

    С --reset: сбрасывает все авто-canonical_id и пересчитывает заново (78% churn!).
    С --revalidate: ТОЧЕЧНО разбивает только те существующие кластеры, где cross-site
    пара конфликтует по текущим guard'ам (закрывает «whack-a-mole» старых матчей без
    churn полного --reset). Ручные матчи (is_manual=True) никогда не трогаются.
    """
    from sqlalchemy import update as sa_update

    Session = storage.make_session()
    with Session() as session:
        lock_taken = _acquire_matcher_lock(session, wait=False)
        if not lock_taken:
            click.echo("Another matcher/rematch is already running; skipped.")
            return
        try:
            if relink_dead:
                plan = matcher.relink_dead_members(session, dry_run=dry_run)
                swaps = [r for r in plan if r["action"] == "swap"]
                skips = [r for r in plan if r["action"] != "swap"]
                click.echo(f"relink-dead: {len(swaps)} swap, {len(skips)} skip")
                for r in swaps:
                    click.echo(
                        f"  cl{r['match_id']} [{r['site']}] dead#{r['old']} → live#{r['new']} (score {r['score']})"
                    )
                for r in skips[:20]:
                    click.echo(f"  cl{r['match_id']} [{r['site']}] dead#{r['old']} — {r['action']}")
                if dry_run:
                    click.echo("(dry-run — ничего не изменено)")
                else:
                    click.echo(
                        f"applied {len(swaps)} swap'ов (swap_alternative → кластер is_manual)"
                    )
                return

            if revalidate:
                # Coherent split (keep largest spec-coherent cross-site group, eject
                # outliers; dissolve only if none). Same logic now auto-runs after
                # match_products in the scrape pipeline.
                actions = matcher.revalidate_split(session, dry_run=dry_run)
                for a in actions:
                    if a["action"] == "dissolve":
                        click.echo(
                            f"  cl{a['match_id']}: DISSOLVE {a['unmatched']}"
                        )
                    else:
                        click.echo(f"  cl{a['match_id']}: KEEP {a['keep']}, EJECT {a['eject']}")
                if dry_run:
                    click.echo(f"(dry-run — {len(actions)} кластеров, ничего не изменено)")
                else:
                    click.echo(f"revalidate: re-split {len(actions)} кластеров (+rejections)")
                return

            if reset:
                # Сброс canonical_id только у авто-матчей
                auto_match_ids = session.scalars(
                    select(storage.Match.id).where(storage.Match.is_manual.is_(False))
                ).all()
                if auto_match_ids:
                    session.execute(
                        sa_update(storage.Product)
                        .where(storage.Product.canonical_id.in_(auto_match_ids))
                        .values(canonical_id=None)
                    )
                    session.execute(
                        sa_update(storage.Match)
                        .where(storage.Match.is_manual.is_(False))
                        .values(needs_review=False)
                    )
                    # Удаляем авто-матчи из matches таблицы
                    for mid in auto_match_ids:
                        m = session.get(storage.Match, mid)
                        if m and not m.is_manual:
                            session.delete(m)
                    session.commit()
                    click.echo(f"Reset {len(auto_match_ids)} auto-matches.")

            # Заново нормализуем name_normalized (с учётом последних изменений пайплайна)
            click.echo("Re-normalizing name_normalized…")
            from src.normalize import normalize_name

            products = session.scalars(select(storage.Product)).all()
            for p in products:
                p.name_normalized = normalize_name(p.name or "")
            session.commit()
            click.echo(f"Re-normalized {len(products)} products.")

            # Запуск матчинга
            thr = threshold if threshold is not None else matcher.FUZZY_THRESHOLD
            click.echo(f"Running matcher (threshold={thr})…")
            clusters = matcher.match_products(session, fuzzy_threshold=thr)
            click.echo(f"Matcher done: {clusters} clusters created/updated.")

            # Флагирование подозрительных расхождений цен
            flagged = matcher.flag_suspected_mismatches(session)
            click.echo(f"Price-spread flags updated: {flagged} matches changed.")
        finally:
            _release_matcher_lock(session)


_VALIDATE_CLIENT_SITE = "pharmonline"  # сайт-клиент — валидировать только с --force


@cli.command("validate-links")
@click.option(
    "--site", default="aptekonline", help="Сайт (default aptekonline); client только с --force"
)
@click.option("--limit", type=int, default=None, help="Макс. товаров (для smoke-теста)")
@click.option("--concurrency", default=5, help="Параллельных HTTP-запросов")
@click.option("--rate-per-sec", default=2.0, help="Лимит запросов/сек (анти-бан)")
@click.option("--matched-only/--all", default=True, help="Только matched товары (default)")
@click.option("--force", is_flag=True, help="Разрешить валидацию сайта-клиента")
def validate_links_cmd(
    site: str,
    limit: int | None,
    concurrency: int,
    rate_per_sec: float,
    matched_only: bool,
    force: bool,
) -> None:
    """HTTP-проверка URL товаров → помечает 404-страницы (Product.url_dead_at).

    aptekonline JSON API листит «фантомные» товары (в каталоге, но страница 404).
    Comparison скрывает помеченные. Запускать только через route, который имеет
    рабочий доступ к сайту (server/proxy path). Rate-limit + circuit-breaker +
    mass-dead cap защищают рабочий route от бана и comparison от обнуления.
    """
    import asyncio

    from sqlalchemy import func

    from src import link_validator
    from src._time import utcnow

    # Allow-list: валидация сайта-КЛИЕНТА скрыла бы его товары из comparison (фильтр
    # в dash_comparison роняет матч без клиента). Требуем явный --force.
    if site == _VALIDATE_CLIENT_SITE and not force:
        raise click.ClickException(
            f"validate-links на сайте-клиенте ({site}) скрыл бы товары клиента из "
            "comparison. Добавь --force если точно нужно."
        )

    Session = storage.make_session()
    with Session() as session:
        q = select(storage.Product).where(storage.Product.site == site)
        if matched_only:
            # matched ИЛИ ранее-помеченные мёртвыми — последним даём шанс на revival
            # (страница вернулась), даже если потеряли матч. Иначе url_dead_at завис
            # бы навсегда (аудит H2/TTL): revival только если товар ещё в check-set.
            q = q.where(
                storage.Product.canonical_id.is_not(None) | storage.Product.url_dead_at.is_not(None)
            )
        q = q.order_by(storage.Product.id)
        if limit:
            q = q.limit(limit)
        products = session.scalars(q).all()
        click.echo(
            f"validate-links: проверяю {len(products)} URL ({site}, "
            f"matched_only={matched_only}, rate={rate_per_sec}/s)…"
        )
        items = [(p.id, p.url) for p in products]
        results, meta = asyncio.run(
            link_validator.check_urls(items, concurrency=concurrency, rate_per_sec=rate_per_sec)
        )
        frac = link_validator.dead_fraction(results)
        # mass-dead guard: на реальном прогоне (≥100 URL) аномальная доля мёртвых =
        # смена URL-схемы / maintenance aptekonline → НЕ применяем (иначе обнулим
        # comparison целиком). На малых smoke-прогонах не срабатывает.
        if meta["checked"] >= 100 and frac > link_validator.MAX_DEAD_FRACTION:
            click.echo(
                f"ABORT: dead_fraction={frac:.0%} > {link_validator.MAX_DEAD_FRACTION:.0%} "
                f"на {meta['checked']} URL — аномалия (смена URL-схемы?). НЕ применяю.",
                err=True,
            )
            raise SystemExit(2)
        counts = link_validator.apply_results(session, results, utcnow())
        session.commit()
        total_dead = session.scalar(
            select(func.count(storage.Product.id)).where(
                storage.Product.site == site, storage.Product.url_dead_at.is_not(None)
            )
        )
        click.echo(
            f"checked={meta['checked']}/{len(items)} aborted={meta['aborted']} "
            f"newly_dead={counts['newly_dead']} revived={counts['revived']} "
            f"still_dead={counts['still_dead']} errors={counts['error']} "
            f"dead_frac={frac:.1%} | total_dead({site})={total_dead}"
        )
        if meta["aborted"]:
            click.echo(
                "WARN: circuit-breaker — серия ошибок (возможен бан/throttle), прогон неполный.",
                err=True,
            )
            raise SystemExit(3)


@cli.command("report")
@click.option("--run-id", type=int, default=None, help="ID прогона (по умолчанию — последний ok)")
@click.option("--send", is_flag=True, help="Отправить по email")
@click.option("--tenant-id", type=int, default=1, show_default=True)
def report_cmd(run_id: int | None, send: bool, tenant_id: int) -> None:
    """Перегенерировать отчёт по существующему прогону."""
    Session = storage.make_session()
    with Session() as session:
        run = None
        if run_id is None:
            recent = session.scalars(
                select(storage.Run)
                .where(
                    storage.Run.status == "ok",
                    storage.Run.tenant_id == tenant_id,
                )
                .order_by(storage.Run.id.desc())
                .limit(100)
            ).all()
            run = (
                next(
                    (item for item in recent if storage.run_is_financially_eligible(item)),
                    None,
                )
                if send
                else (recent[0] if recent else None)
            )
            if not run:
                message = (
                    "No financially eligible full-catalog runs found."
                    if send
                    else "No successful runs found."
                )
                raise click.ClickException(message)
            run_id = run.id
        else:
            run = session.get(storage.Run, run_id)
            if run is None:
                raise click.ClickException(f"Run #{run_id} not found.")
            if run.tenant_id != tenant_id:
                raise click.ClickException(f"Run #{run_id} does not belong to tenant #{tenant_id}.")

        if send:
            from src import roi as roi_mod

            if tenant_id != 1:
                raise click.ClickException(
                    "Legacy scrape-report email delivery is only configured for tenant #1."
                )

            if not storage.run_is_financially_eligible(run):
                raise click.ClickException(
                    f"Run #{run_id} is not a verified full-catalog run; email blocked."
                )
            if not roi_mod.financial_inputs_are_fresh(
                session,
                tenant_id=run.tenant_id,
            ):
                raise click.ClickException(
                    "Fresh verified full-catalog inputs are missing for one or more sites; "
                    "email blocked."
                )

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


def _sync_pharmonline_categories(session, discovered: list[tuple[str, str]]) -> tuple[int, int]:
    """Upsert обнаруженных (slug, name) категорий pharmonline. Уже замапленные
    (по pharmonline_slug) пропускаются. key = `pharma_{slug}` (конвенция проекта).
    Возвращает (добавлено, пропущено). Чистая функция — тестируется без сети.
    """
    existing = {
        c.pharmonline_slug
        for c in session.scalars(
            select(storage.Category).where(storage.Category.pharmonline_slug.is_not(None))
        )
    }
    added = skipped = 0
    for slug, name in discovered:
        slug = (slug or "").strip()
        if not slug or slug in existing:
            skipped += 1
            continue
        watchlist.add_category(
            session,
            key=f"pharma_{slug}",
            label_ru=(name or slug),
            label_az=(name or slug),
            pharmonline_slug=slug,
        )
        existing.add(slug)
        added += 1
    return added, skipped


@category_group.command("sync-pharmonline")
@click.option("--dry-run", is_flag=True, help="Только показать, сколько добавится")
def category_sync_pharmonline(dry_run: bool) -> None:
    """Авто-обнаружить ВСЕ категории pharmonline через DDP `getFilterParam` и
    засеять недостающие в БД.

    Чинит неполное покрытие: было заведено 53 категории из 205 на сайте → ночной
    прогон видел ~половину каталога. После sync прогон покрывает весь pharmonline.
    Требует IPROYAL_* + PHARMONLINE_USE_DDP в окружении (residential-прокси для DDP).
    """
    import asyncio

    from src.scrapers.pharmonline_ddp import PharmonlineDDPScraper

    storage.init_db()

    async def _fetch() -> list[tuple[str, str]]:
        async with PharmonlineDDPScraper() as sc:
            flt = await sc._ddp.call(
                "getFilterParam",
                [{"query": {}, "sortBy": {"totalMinPrice": 1}, "productLimit": 24}, sc._locale],
                timeout=30.0,
            )
            return [
                (c.get("path"), c.get("name")) for c in (flt.get("category") or []) if c.get("path")
            ]

    discovered = asyncio.run(_fetch())
    click.echo(f"DDP getFilterParam вернул {len(discovered)} категорий pharmonline")

    Session = storage.make_session()
    with Session() as s:
        if dry_run:
            existing = {
                c.pharmonline_slug
                for c in s.scalars(
                    select(storage.Category).where(storage.Category.pharmonline_slug.is_not(None))
                )
            }
            new = [sl for sl, _ in discovered if sl and sl not in existing]
            click.echo(
                f"[dry-run] добавилось бы новых: {len(new)} (уже есть: {len(discovered) - len(new)})"
            )
            return
        added, skipped = _sync_pharmonline_categories(s, discovered)
        click.echo(f"Добавлено новых категорий: {added}, пропущено (уже замаплены): {skipped}")


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
            click.echo(
                "(watchlist пуст — добавь товары через `watchlist add` или `watchlist import`)"
            )
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
@click.option("--site", required=True, type=click.Choice(list(watchlist.SITES)))
@click.option("--url", required=True)
@click.option(
    "--status", default="confirmed", type=click.Choice(["pending", "confirmed", "not_found"])
)
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
