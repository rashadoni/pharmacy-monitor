"""Health-check логика: детекция проблем с прогонами и алерт по email.

Используется через CLI `pharmacy-monitor health-check` (на cron каждый час)
или встраивается в основной прогон.

Что детектируется:
1. **Stale**: последний успешный run был >max_age_hours назад
2. **Failed**: последний run завершился со status='failed'
3. **Empty**: последний run ok но < min_products (полностью пустой)
4. **Site-drop**: какой-то сайт спарсил <50% от baseline (медианы за 7 дней)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from src._time import utcnow
from typing import Literal

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from src.storage import PriceSnapshot, Product, Run

log = structlog.get_logger()

Severity = Literal["ok", "warning", "critical"]


@dataclass
class HealthIssue:
    severity: Severity
    code: str
    message: str
    context: dict = field(default_factory=dict)


@dataclass
class HealthReport:
    status: Severity  # совокупный наихудший
    issues: list[HealthIssue] = field(default_factory=list)
    last_run_id: int | None = None
    last_run_at: datetime | None = None
    last_run_status: str | None = None

    @property
    def is_healthy(self) -> bool:
        return self.status == "ok"


def check_health(
    session: Session,
    *,
    max_age_hours: int = 26,  # с запасом за суточный cron + jitter
    min_products: int = 1,
    site_drop_threshold: float = 0.5,  # 50% от медианы
    history_days: int = 7,
) -> HealthReport:
    """Запустить все проверки и вернуть совокупный отчёт."""
    report = HealthReport(status="ok")

    last_run = session.scalars(select(Run).order_by(desc(Run.started_at)).limit(1)).first()

    if last_run is None:
        report.status = "warning"
        report.issues.append(
            HealthIssue(
                "warning",
                "no_runs",
                "В БД ни одного прогона. Запустите `pharmacy-monitor run`.",
            )
        )
        return report

    report.last_run_id = last_run.id
    report.last_run_at = last_run.started_at
    report.last_run_status = last_run.status

    # 1. Stale check — относительно сейчас
    age = utcnow() - last_run.started_at
    if age > timedelta(hours=max_age_hours):
        report.issues.append(
            HealthIssue(
                "critical",
                "stale_run",
                f"Последний прогон был {age.total_seconds() / 3600:.1f}ч назад "
                f"(порог {max_age_hours}ч). Проверьте cron.",
                context={"hours_ago": age.total_seconds() / 3600},
            )
        )

    # 2. Failed check
    if last_run.status == "failed":
        report.issues.append(
            HealthIssue(
                "critical",
                "last_run_failed",
                f"Последний прогон #{last_run.id} упал: {last_run.error_message or '?'}",
                context={"run_id": last_run.id, "error": last_run.error_message},
            )
        )

    # 3. Empty check (только если статус ok)
    if last_run.status == "ok" and (last_run.products_scraped or 0) < min_products:
        report.issues.append(
            HealthIssue(
                "critical",
                "empty_run",
                f"Прогон #{last_run.id} закончился ok, но спарсил {last_run.products_scraped} "
                f"товаров (порог {min_products}). Возможно сайты сменили вёрстку.",
                context={"run_id": last_run.id, "products": last_run.products_scraped},
            )
        )

    # 4. Site-drop check (сравниваем с медианой за history_days)
    if last_run.status == "ok":
        report.issues.extend(
            _check_site_drops(session, last_run.id, site_drop_threshold, history_days)
        )
        # 5. Sanity: цены не должны быть все нулевыми / NaN
        report.issues.extend(_check_zero_prices(session, last_run.id))
        # 6. Brand-coverage drop (если предыдущий имел brand'ы, а сейчас нет — алерт)
        report.issues.extend(_check_brand_coverage_drop(session, last_run.id))

    # 7. Per-site silence — `stale_run` смотрит только на ПОСЛЕДНИЙ run в БД, но
    # один сайт может молчать неделю пока другие отрабатывают. Например pharm/apt
    # на Mac launchd, aloe на проде — если Mac уснул, aloe-run всё равно свежий,
    # и stale_run check ничего не скажет о pharm/apt. Проверяем каждый сайт
    # отдельно.
    report.issues.extend(_check_site_silence(session, max_age_hours))

    # Совокупный статус
    if any(i.severity == "critical" for i in report.issues):
        report.status = "critical"
    elif any(i.severity == "warning" for i in report.issues):
        report.status = "warning"

    return report


def _check_site_drops(
    session: Session,
    last_run_id: int,
    threshold: float,
    history_days: int,
) -> list[HealthIssue]:
    """Для каждого сайта — сколько ИЗ КАТАЛОГА увидели в этом прогоне.

    Diff-only-aware (2026-05-09): после оптимизации persist'а snapshot
    пишется только при изменении цены. Раньше считали `COUNT(snapshots)`
    per (run, site), что после diff-only стало = «продукты с изменением
    цены на этом сайте сегодня», не «увиденные на этом сайте сегодня»
    (последнее обычно ~1000, первое ~30).

    Корректная метрика: `Product.last_seen_at >= run.started_at` per site.
    Сравниваем с total products per site (известный каталог). Если видимо
    < threshold доли каталога — alert.
    """
    sites = ["pharmonline", "aptekonline", "aloe"]
    issues: list[HealthIssue] = []

    last_run = session.get(Run, last_run_id)
    if last_run is None:
        return []

    for site in sites:
        total = (
            session.scalar(select(func.count()).select_from(Product).where(Product.site == site))
            or 0
        )
        if total < 10:
            continue  # сайт ещё не наполнен — не на чем сравнивать

        # Видимы в этом прогоне: last_seen_at >= run.started_at. Verхняя
        # граница не ставится: check_health всегда вызывается для ПОСЛЕДНЕГО
        # run'а, более новых ещё нет, поэтому overestimate невозможен.
        seen = (
            session.scalar(
                select(func.count())
                .select_from(Product)
                .where(
                    Product.site == site,
                    Product.last_seen_at >= last_run.started_at,
                )
            )
            or 0
        )

        ratio = seen / total
        if ratio < threshold:
            issues.append(
                HealthIssue(
                    "warning" if ratio > 0.2 else "critical",
                    "site_drop",
                    f"Сайт {site}: увидели {seen}/{total} товаров каталога "
                    f"({ratio * 100:.0f}%). Порог: {threshold * 100:.0f}%. "
                    f"Возможно сменилась вёрстка или сайт лежал.",
                    context={
                        "site": site,
                        "seen": seen,
                        "total": total,
                        "ratio": ratio,
                    },
                )
            )
    return issues


def _check_zero_prices(session: Session, run_id: int) -> list[HealthIssue]:
    """Если >50% snapshots в прогоне имеют price=0/NULL — скрейпер сломан."""
    total = (
        session.scalar(
            select(func.count()).select_from(PriceSnapshot).where(PriceSnapshot.run_id == run_id)
        )
        or 0
    )
    if total < 10:
        return []  # маленький прогон — недостаточно данных для проверки
    zero_or_null = (
        session.scalar(
            select(func.count())
            .select_from(PriceSnapshot)
            .where(
                PriceSnapshot.run_id == run_id,
                (PriceSnapshot.price.is_(None)) | (PriceSnapshot.price <= 0),
            )
        )
        or 0
    )
    ratio = zero_or_null / total
    if ratio < 0.50:
        return []
    return [
        HealthIssue(
            "critical",
            "zero_prices",
            f"{zero_or_null}/{total} snapshots с price=0 или NULL ({ratio * 100:.0f}%). "
            f"Скорее всего — сломан price-парсинг.",
            context={"zero_count": zero_or_null, "total": total, "ratio": ratio},
        )
    ]


def _check_brand_coverage_drop(session: Session, run_id: int) -> list[HealthIssue]:
    """Регрессия brand-coverage: rest-of-catalog ≥50%, visible-now <20% → alert.

    Diff-only-aware (2026-05-09): сравниваем visible-in-curr-run
    (`last_seen_at >= run.started_at`) с **остальным каталогом**
    (продукты, которых не видели сегодня). Это устраняет смещение
    от продуктов текущего прогона в общей оценке ковеража.
    """
    last_run = session.get(Run, run_id)
    if last_run is None:
        return []

    # Видимые в этом прогоне
    curr_total = (
        session.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.last_seen_at >= last_run.started_at)
        )
        or 0
    )
    if curr_total < 10:
        return []
    curr_with_brand = (
        session.scalar(
            select(func.count())
            .select_from(Product)
            .where(
                Product.last_seen_at >= last_run.started_at,
                Product.brand.is_not(None),
            )
        )
        or 0
    )
    curr_coverage = curr_with_brand / curr_total

    # Остальной каталог (last_seen_at до run.started_at)
    rest_total = (
        session.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.last_seen_at < last_run.started_at)
        )
        or 0
    )
    if rest_total < 10:
        return []
    rest_with_brand = (
        session.scalar(
            select(func.count())
            .select_from(Product)
            .where(
                Product.last_seen_at < last_run.started_at,
                Product.brand.is_not(None),
            )
        )
        or 0
    )
    rest_coverage = rest_with_brand / rest_total

    # Триггер: остальной каталог имеет brand'ы, но текущий прогон их не извлёк.
    if rest_coverage >= 0.50 and curr_coverage < 0.20:
        return [
            HealthIssue(
                "critical",
                "brand_coverage_loss",
                f"Brand-coverage в этом прогоне: {curr_coverage * 100:.0f}% "
                f"({curr_with_brand}/{curr_total}). В остальном каталоге: "
                f"{rest_coverage * 100:.0f}%. Скрейпер перестал извлекать brand — "
                f"возможно сменилась вёрстка.",
                context={
                    "curr_coverage": round(curr_coverage, 3),
                    "rest_coverage": round(rest_coverage, 3),
                },
            )
        ]
    return []


# Per-site пороги «молчания» (часы). Дефолт = суточная частота (26ч = 24ч + jitter).
# Сайты с НЕ-суточным расписанием переопределяются: aptekonline скрейпится РАЗ В
# НЕДЕЛЮ (Decodo, Пн 02:00 UTC) — «молчит» только если нет обновлений >8 дней,
# иначе hourly health-check спамил бы critical 6 из 7 дней (alert fatigue, маскирует
# реальные сбои Decodo/баланса).
# TODO (при отключении Mac launchd): pharmonline идёт Пн/Ср/Пт, зазор Пт→Пн ~72ч →
# поднять его порог до ~80ч. Сейчас НЕ переопределяем: Mac ежедневно обновляет
# pharmonline last_seen (DR-фоллбэк ещё включён), поэтому суточные 26ч корректны.
_SITE_MAX_AGE_HOURS: dict[str, int] = {
    "aptekonline": 8 * 24 + 6,  # 198ч = 8 суток + 6ч jitter (недельный таймер)
}


def _check_site_silence(session: Session, max_age_hours: int = 26) -> list[HealthIssue]:
    """Per-site freshness: если у сайта нет свежих продуктов за порог → alert.

    Метрика: `MAX(Product.last_seen_at)` per site. Если самое свежее обновление
    у сайта старше порога, значит скрейп этого сайта молчит — прокси упал, баланс
    кончился, или CF забанил. Порог per-site (`_SITE_MAX_AGE_HOURS`, fallback на
    `max_age_hours`) — у сайтов разное расписание (см. константу выше). `stale_run`
    смотрит на ПОСЛЕДНИЙ run в принципе, но не per-site.

    Сайты с 0 продуктов в каталоге игнорируются (новый/выключенный).
    """
    issues: list[HealthIssue] = []
    now = utcnow()

    for site in ("pharmonline", "aptekonline", "aloe"):
        # Если сайт ещё ни разу не скрейпился — пропустить
        total = (
            session.scalar(select(func.count()).select_from(Product).where(Product.site == site))
            or 0
        )
        if total == 0:
            continue

        last_seen = session.scalar(
            select(func.max(Product.last_seen_at)).where(Product.site == site)
        )

        site_max = _SITE_MAX_AGE_HOURS.get(site, max_age_hours)
        cutoff = now - timedelta(hours=site_max)
        if last_seen is None or last_seen < cutoff:
            age_h = (now - last_seen).total_seconds() / 3600 if last_seen else 24 * 7
            issues.append(
                HealthIssue(
                    "critical",
                    "site_silent",
                    f"Сайт {site}: последнее обновление {age_h:.1f}ч назад "
                    f"(порог {site_max}ч). Прокси упал / баланс кончился?",
                    context={
                        "site": site,
                        "hours_silent": round(age_h, 1),
                        "threshold_hours": site_max,
                    },
                )
            )
    return issues


def render_alert_html(report: HealthReport) -> str:
    """Простое HTML-письмо для алерта."""
    color = {"ok": "#34c759", "warning": "#ff9500", "critical": "#ff3b30"}[report.status]
    title = {"ok": "✓ OK", "warning": "⚠️ Внимание", "critical": "🔴 Проблема"}[report.status]

    rows = []
    for i in report.issues:
        sev_color = {"warning": "#ff9500", "critical": "#ff3b30", "ok": "#34c759"}[i.severity]
        rows.append(
            f"<tr><td style='padding:8px;border-bottom:1px solid #eee;'>"
            f"<span style='color:{sev_color};font-weight:600;'>{i.severity.upper()}</span> "
            f"<code style='font-size:11px;color:#86868b;'>{i.code}</code></td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee;'>{i.message}</td></tr>"
        )

    return f"""
    <div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px;margin:24px auto;
                background:#fff;border-radius:12px;border:1px solid #e5e5e7;overflow:hidden;">
      <div style="padding:18px 24px;border-bottom:1px solid #f0f0f3;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#86868b;">
          Pharmacy Monitor — Health Check
        </div>
        <div style="font-size:22px;font-weight:600;color:{color};margin-top:4px;">
          {title}
        </div>
        <div style="font-size:13px;color:#6e6e73;margin-top:6px;">
          Last run: #{report.last_run_id or "—"} ({report.last_run_status or "—"}) at
          {report.last_run_at.strftime("%Y-%m-%d %H:%M UTC") if report.last_run_at else "—"}
        </div>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        {"".join(rows) if rows else '<tr><td style="padding:16px;color:#34c759;">All checks pass.</td></tr>'}
      </table>
      <div style="padding:12px 24px;background:#fafafa;font-size:11px;color:#86868b;">
        Сгенерировано {utcnow().strftime("%Y-%m-%d %H:%M UTC")}
      </div>
    </div>
    """
