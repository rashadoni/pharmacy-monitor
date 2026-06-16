"""Health-check логика: детекция проблем с прогонами и алерт по email.

Используется через CLI `pharmacy-monitor health-check` (на cron каждый час)
или встраивается в основной прогон.

Что детектируется:
1. **Stale**: последний успешный run был >max_age_hours назад
2. **Failed**: последний run завершился со status='failed'
3. **Empty**: последний run ok но < min_products (полностью пустой)
4. **Site-drop**: сайт покрыл <50% живого каталога за окно покрытия
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
    site_drop_threshold: float = 0.5,  # порог: доля живого каталога за окно покрытия
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

    # 4. Site-drop check — доля живого каталога, покрытая за окно (см. _check_site_drops)
    if last_run.status == "ok":
        report.issues.extend(_check_site_drops(session, last_run.id, site_drop_threshold))
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


_SITE_FRESHNESS_DAYS: dict[str, int] = {
    "pharmonline": 21,  # недельный таймер → ~3 цикла
    "aptekonline": 21,  # недельный таймер
    "aloe": 10,  # ежедневный → запас на простой
}
_DEFAULT_FRESHNESS_DAYS = 21

# Окно ПОКРЫТИЯ для числителя site_drop: сколько РАЗНЫХ товаров сайта видели за
# последние N дней (≥ один полный цикл скрейпа + запас). Берём окно, а НЕ
# буквальный последний прогон — последним бывает ЧАСТИЧНЫЙ intraday-прогон
# (pharmonline run_277 = 127 товаров featured-выборки), тогда seen=127/каталог
# дал бы ложный site_drop. Окно включает последний ПОЛНЫЙ прогон → одиночный
# частичный прогон метрику не роняет. Должно быть < _SITE_FRESHNESS_DAYS.
_SITE_COVERAGE_DAYS: dict[str, int] = {
    "pharmonline": 14,  # недельный цикл (7д) + запас на слип/пропуск одного прогона
    "aptekonline": 14,  # недельный цикл + запас
    "aloe": 4,  # дневной цикл + запас
}
_DEFAULT_COVERAGE_DAYS = 14


def _check_site_drops(
    session: Session,
    last_run_id: int,
    threshold: float,
) -> list[HealthIssue]:
    """Per-site: какую долю ЖИВОГО каталога покрыли НЕДАВНИЕ скрейпы.

    ЧИСЛИТЕЛЬ `seen` = distinct товаров сайта, виденных за окно ПОКРЫТИЯ
    (`_SITE_COVERAGE_DAYS`, ≥ один цикл скрейпа) — а НЕ за буквальный последний
    прогон: последним бывает ЧАСТИЧНЫЙ intraday-прогон (pharmonline run_277 =
    127 товаров featured-выборки), и `seen=127` дал бы ложный site_drop. Окно
    покрытия включает последний ПОЛНЫЙ прогон → одиночный частичный прогон
    метрику не роняет.

    ЗНАМЕНАТЕЛЬ `total` = ЖИВОЙ каталог (виден за `_SITE_FRESHNESS_DAYS`), а НЕ
    весь накопленный total: «мёртвые» ряды (делистинг, ротация, осиротевшие дубли
    другого скрейпера с иным external_id) иначе занижают ratio.

    Реальные кейсы (2026-06-16): (1) pharmonline 9840/19811=49% из-за ~9.8K
    Playwright-дублей при живом каталоге ~9951; (2) после их чистки — 127/10333=1%
    из-за частичного run_277. Окно покрытия (14д) + свежести (21д) → ~99%.
    """
    sites = ["pharmonline", "aptekonline", "aloe"]
    issues: list[HealthIssue] = []

    last_run = session.get(Run, last_run_id)
    if last_run is None:
        return []
    # Прогон ещё идёт (finished_at пуст) → частичное покрытие это НЕ «падение»
    # (health-check мог сработать в середине скрейпа). Дождёмся завершения.
    if last_run.finished_at is None:
        return []

    now = utcnow()
    for site in sites:
        # Знаменатель = живой каталог за окно свежести (см. docstring).
        fresh_cutoff = now - timedelta(days=_SITE_FRESHNESS_DAYS.get(site, _DEFAULT_FRESHNESS_DAYS))
        total = (
            session.scalar(
                select(func.count())
                .select_from(Product)
                .where(Product.site == site, Product.last_seen_at >= fresh_cutoff)
            )
            or 0
        )
        if total < 10:
            continue  # сайт ещё не наполнен / давно молчит — не на чем сравнивать

        # Числитель = distinct товаров, виденных за окно ПОКРЫТИЯ (не за один
        # последний прогон — он бывает частичным intraday, см. docstring).
        cover_cutoff = now - timedelta(days=_SITE_COVERAGE_DAYS.get(site, _DEFAULT_COVERAGE_DAYS))
        seen = (
            session.scalar(
                select(func.count())
                .select_from(Product)
                .where(
                    Product.site == site,
                    Product.last_seen_at >= cover_cutoff,
                )
            )
            or 0
        )

        if seen == 0:
            # Сайт не скрейпился ни разу за окно покрытия → это staleness, ловит
            # site_silent (per-site порог), а не этот чек (он про «скрейпился,
            # но недобрал каталог»).
            continue

        ratio = seen / total
        if ratio < threshold:
            issues.append(
                HealthIssue(
                    "warning" if ratio > 0.2 else "critical",
                    "site_drop",
                    f"Сайт {site}: за окно покрытия видели {seen}/{total} живого "
                    f"каталога ({ratio * 100:.0f}%). Порог: {threshold * 100:.0f}%. "
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
    # pharmonline тоже недельный таймер (Mon 01:00). Без этого override default
    # 26ч давал бы ложный site_silent 6 из 7 дней, как только Mac-DR-фолбэк
    # (ежедневно освежающий pharmonline) будет отключён. Инертен пока Mac жив.
    "pharmonline": 8 * 24 + 6,
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
