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
    # один сайт может молчать неделю пока другие отрабатывают. Например aloe-run
    # может быть свежим, а недельный pharmonline/aptekonline timer не обновлялся.
    # Проверяем каждый сайт отдельно.
    report.issues.extend(_check_site_silence(session, max_age_hours))

    # 8. Полный отказ сайта: последний прогон, включавший сайт, собрал РОВНО 0
    # товаров → critical сразу (в течение часа), не дожидаясь суточного порога
    # site_silent. Раньше это терялось: smoke-test пропускал `current == 0`, а
    # `empty_run` смотрит только на последний прогон в принципе (его маскировал
    # intraday-прогон другого сайта).
    report.issues.extend(_check_site_zero_scrape(session))

    # Совокупный статус
    if any(i.severity == "critical" for i in report.issues):
        report.status = "critical"
    elif any(i.severity == "warning" for i in report.issues):
        report.status = "warning"

    return report


def alert_signature(report: HealthReport) -> str:
    """Стабильная подпись набора АКТИВНЫХ проблем (code+site) — для дедупа алертов.

    Одинаковый набор проблем → одинаковая подпись. Изменился набор (появилась/ушла
    проблема) → подпись другая → алерт уходит сразу (новую проблему не глушим).
    """
    parts = sorted(
        f"{i.code}:{i.context.get('site', '')}"
        for i in report.issues
        if i.severity in ("warning", "critical")
    )
    return "|".join(parts)


def alert_due(
    signature: str,
    last_state: dict | None,
    *,
    now: datetime,
    cooldown_hours: float,
) -> bool:
    """Слать ли email-алерт сейчас (анти-спам для hourly health-check).

    - набор проблем ИЗМЕНИЛСЯ (подпись другая) → слать (новую проблему не глушим);
    - тот же набор, прошёл `cooldown_hours` с прошлой отправки → слать (напоминание);
    - тот же набор, в пределах cooldown → НЕ слать (раньше слали каждый час = спам).
    """
    if not last_state or last_state.get("signature") != signature:
        return True
    sent_at = last_state.get("sent_at")
    if not sent_at:
        return True
    try:
        last_dt = datetime.fromisoformat(sent_at)
    except (TypeError, ValueError):
        return True
    return (now - last_dt) >= timedelta(hours=cooldown_hours)


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


# Минимальная доля каталога доминирующего сайта, при которой прогон считается
# РЕПРЕЗЕНТАТИВНЫМ для оценки brand-coverage. Частичные intraday-тики (pharmonline
# featured ~100-250 шт из ~10k каталога) НЕ репрезентативны: их срез смещён в
# косметику/коммодити без brand (категория dish-mecunlar и т.п.) → давали ложный
# critical. Реальный кейс (2026-06-21, run #307): 116 товаров featured-тика, 20%
# brand (23/116) vs 89% по полному каталогу pharmonline → ложная «сменилась вёрстка».
# Тот же класс false-positive, что уже закрыт в _check_site_drops/_check_site_zero_scrape
# («intraday-блипы частичные НЕ триггерят»). 0.25 отделяет featured-тик (~1% каталога)
# от полного прогона (aloe ~100%, pharmonline ~97%, aptek >100%).
_BRAND_COVERAGE_MIN_RUN_FRACTION = 0.25


def _check_brand_coverage_drop(session: Session, run_id: int) -> list[HealthIssue]:
    """Регрессия brand-coverage: rest-of-catalog ≥50%, visible-now <20% → alert.

    Diff-only-aware (2026-05-09): сравниваем visible-in-curr-run
    (`last_seen_at >= run.started_at`) с **остальным каталогом**
    (продукты, которых не видели сегодня). Это устраняет смещение
    от продуктов текущего прогона в общей оценке ковеража.

    Intraday-guard (2026-06-21): пропускаем оценку, если прогон — частичный
    intraday-тик (когорта покрывает < `_BRAND_COVERAGE_MIN_RUN_FRACTION` каталога
    доминирующего сайта). Featured-тик pharmonline (~116 косметик-товаров без brand)
    давал ложный critical, хотя полный каталог на 89%.
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

    # Репрезентативность: частичный intraday-тик судить по brand-coverage нельзя.
    # Доминирующий сайт когорты + его каталог; если когорта — тонкий срез, скип.
    dominant = session.execute(
        select(Product.site, func.count())
        .where(Product.last_seen_at >= last_run.started_at)
        .group_by(Product.site)
        .order_by(func.count().desc(), Product.site)  # site = детерминированный tie-break
        .limit(1)
    ).first()
    if dominant is not None:
        dom_site, _dom_count = dominant
        site_catalog = (
            session.scalar(
                select(func.count()).select_from(Product).where(Product.site == dom_site)
            )
            or 0
        )
        if site_catalog > 0 and curr_total < _BRAND_COVERAGE_MIN_RUN_FRACTION * site_catalog:
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
_SITE_MAX_AGE_HOURS: dict[str, int] = {
    "aptekonline": 8 * 24 + 6,  # 198ч = 8 суток + 6ч jitter (недельный таймер)
    # pharmonline тоже недельный таймер (Mon 01:00). Без этого override default
    # 26ч давал бы ложный site_silent 6 из 7 дней.
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


def _check_site_zero_scrape(session: Session) -> list[HealthIssue]:
    """Per-site: последний прогон, ВКЛЮЧАВШИЙ сайт, собрал РОВНО 0 товаров → critical.

    Ловит полный отказ сайта (лёг / прокси умер / сменилась вёрстка) в течение
    часа (hourly health-check + --alert-email), не дожидаясь порога site_silent
    (~сутки). Реальный кейс (2026-06-11): aloe.az отдал HTTP 502 → прогон собрал
    0 товаров, но не алертнул — `_smoke_test_per_site_coverage` пропускал
    `current == 0` (самый худший случай!), а `empty_run` маскировался intraday.

    Порог именно ==0: intraday-блипы (частичные ~100-250 шт) НЕ триггерят. Берём
    последний прогон С ЭТИМ сайтом в `products_per_site`, поэтому intraday-прогон
    другого сайта (pharmonline featured) не маскирует 0 у aloe/aptek.
    """
    sites = {"pharmonline", "aptekonline", "aloe"}
    issues: list[HealthIssue] = []
    seen: set[str] = set()
    # limit с запасом: intraday-прогоны (hourly, pharmonline) плодят ~24 Run/день,
    # 500 покрывает >2 недель → достаёт даже недельный прогон aptekonline.
    recent = session.scalars(
        select(Run).where(Run.finished_at.isnot(None)).order_by(desc(Run.started_at)).limit(500)
    ).all()
    for run in recent:
        pps = run.products_per_site or {}
        for site in sites - seen:
            if site in pps:
                seen.add(site)
                if (pps.get(site) or 0) == 0:
                    issues.append(
                        HealthIssue(
                            "critical",
                            "site_zero_scrape",
                            f"Сайт {site}: последний прогон #{run.id} собрал 0 товаров "
                            f"(сайт лёг / прокси упал / сменилась вёрстка?).",
                            context={"site": site, "run_id": run.id},
                        )
                    )
        if seen == sites:
            break
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
