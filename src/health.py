"""Health-check логика: детекция проблем с прогонами и алерт по email.

Используется через CLI `pharmacy-monitor health-check` (на cron каждый час)
или встраивается в основной прогон.

Что детектируется:
1. **Stale**: последнего прогона нет дольше ритма самого частого сайта
2. **Failed**: последний run завершился со status='failed'
3. **Empty**: последний run ok но < min_products (полностью пустой)
4. **Site-drop**: сайт покрыл <50% живого каталога за окно покрытия
5. **Catalog floor**: каталогу pharmonline осталось мало до нижней границы
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from src._time import utcnow
from src.cadence import (
    SITE_SCRAPE_CADENCE_HOURS,
    site_cadence_days,
    site_max_age_hours,
)
from src.storage import PriceSnapshot, Product, Run

log = structlog.get_logger()

Severity = Literal["ok", "warning", "critical"]
HealthAlertAction = Literal["incident", "reminder", "recovery", "notice"]

# Как часто письмо напоминает о долгом предупреждении (`HealthIssue.standing`).
# Неделя, а не сутки, как у инцидента: между полными сборами нового замера нет,
# а висит такое предупреждение месяцами.
STANDING_REMINDER_HOURS = 7 * 24


@dataclass
class HealthIssue:
    severity: Severity
    code: str
    message: str
    context: dict = field(default_factory=dict)
    # Долгое предупреждение: известно заранее, висит неделями и поломкой не
    # является (запас до нижней границы каталога). В отчёте и в статусе оно как
    # любое другое, а в письмах идёт своей дорожкой — см. health_alert_decision.
    # Действует только на warning: critical требует действий сейчас и остаётся
    # инцидентом, как бы его ни пометили.
    standing: bool = False

    @property
    def is_standing(self) -> bool:
        return self.standing and self.severity == "warning"


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

    @property
    def has_incident(self) -> bool:
        """Есть ли в отчёте что-то кроме долгих предупреждений."""
        if self.status == "ok":
            return False
        active = [issue for issue in self.issues if issue.severity in ("warning", "critical")]
        return not active or any(not issue.is_standing for issue in active)


@dataclass(frozen=True)
class HealthAlertDecision:
    """One transition in the persisted health-email state machine."""

    action: HealthAlertAction | None
    signature: str | None = None
    previous_signature: str | None = None
    # Долгие предупреждения, попавшие в это письмо: им записывается время отправки.
    standing_keys: tuple[str, ...] = ()


def check_health(
    session: Session,
    *,
    max_age_hours: int = 26,  # порог для сайта без объявленного ритма (суточный cron + jitter)
    min_products: int = 1,
    site_drop_threshold: float = 0.5,  # порог: доля живого каталога за окно покрытия
) -> HealthReport:
    """Запустить все проверки и вернуть совокупный отчёт."""
    report = HealthReport(status="ok")

    from src import storage

    last_run = storage.latest_terminal_run(session, tenant_id=1)

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

    # 1. Stale check — относительно сейчас. Порог — ритм самого частого сайта:
    # при недельном сборе плановый запуск в остальные ночи выходит без прогона
    # (гвард ритма в `run`), а частичные тики прогон дают не каждый день. С
    # прежними 26ч исправный недельный график сам поднимал бы эту тревогу.
    stale_after_hours = max(max_age_hours, min(_SITE_MAX_AGE_HOURS.values(), default=0))
    age = utcnow() - last_run.started_at
    if age > timedelta(hours=stale_after_hours):
        report.issues.append(
            HealthIssue(
                "critical",
                "stale_run",
                f"Последний прогон был {age.total_seconds() / 3600:.1f}ч назад "
                f"(порог {stale_after_hours}ч). Проверьте cron.",
                context={"hours_ago": age.total_seconds() / 3600},
            )
        )

    # A later partial tick may be operationally healthy, but it must never
    # clear the trust failure of the most recent full-catalog attempt.
    full_attempts = storage.latest_full_catalog_attempts_by_site(
        session,
        storage.FULL_CATALOG_SITES,
        tenant_id=1,
    )
    verified_catalogs = _latest_verified_catalogs_by_site(session, storage)
    fresh_verified_catalogs = {
        site: run
        for site, run in verified_catalogs.items()
        if _run_age_hours(run) <= _SITE_MAX_AGE_HOURS.get(site, max_age_hours)
    }

    # Сторож ритма. Гвард в `run` пропускает лишние ночи молча (exit 0, без
    # прогона), а `site_silent` за этим не уследит: частичный тик освежает
    # `last_seen_at` и без полного сбора. Поэтому возраст последнего
    # ПОДТВЕРЖДЁННОГО каталога сверяем с ритмом сайта напрямую — и неважно, чем
    # кончилась самая свежая попытка: неделя degraded-повторов оставляет данные
    # такими же старыми, как неделя пропусков.
    for site, run in verified_catalogs.items():
        if site in fresh_verified_catalogs:
            continue
        overdue_after = _SITE_MAX_AGE_HOURS.get(site, max_age_hours)
        age_hours = _run_age_hours(run)
        report.issues.append(
            HealthIssue(
                "critical",
                "full_catalog_overdue",
                f"Подтверждённого полного сбора {site} нет {age_hours:.0f}ч "
                f"(порог {overdue_after}ч): плановый сбор не сработал, не прошёл "
                "проверку или ещё идёт.",
                context={
                    "site": site,
                    "run_id": run.id,
                    "hours_ago": age_hours,
                    "threshold_hours": overdue_after,
                },
            )
        )

    # 2. Failed/degraded check. A bounded/watchlist failure is never a
    # catalog epoch. Equally, a rejected full refresh must stay visible but
    # may not be escalated to an outage while the same sites still have a
    # recent, financially verified catalog. This prevents transient Decodo
    # cache/pagination failures from claiming that client data disappeared.
    last_run_has_fresh_verified_catalog = _run_has_fresh_verified_catalogs(
        last_run,
        fresh_verified_catalogs,
    )
    if last_run.status == "failed":
        failed_severity: Severity = (
            "warning"
            if last_run.catalog_scope == "partial" or last_run_has_fresh_verified_catalog
            else "critical"
        )
        report.issues.append(
            HealthIssue(
                failed_severity,
                "last_run_failed",
                f"Последний прогон #{last_run.id} завершился со статусом "
                f"{last_run.status}: {last_run.error_message or '?'}",
                context={
                    "run_id": last_run.id,
                    "error": last_run.error_message,
                    "catalog_scope": last_run.catalog_scope,
                    "fresh_verified_catalog": last_run_has_fresh_verified_catalog,
                },
            )
        )

    if last_run.status == "degraded":
        quality = last_run.run_quality or {}
        degraded_severity: Severity = (
            "warning"
            if last_run_has_fresh_verified_catalog
            or not (last_run.catalog_scope == "full" and not last_run.catalog_verified)
            else "critical"
        )
        report.issues.append(
            HealthIssue(
                degraded_severity,
                "last_run_degraded",
                f"Прогон #{last_run.id} завершён частично: "
                f"{last_run.error_message or 'см. run_quality'}",
                context={
                    "run_id": last_run.id,
                    "run_quality": quality,
                    "fresh_verified_catalog": last_run_has_fresh_verified_catalog,
                },
            )
        )
        for site, details in (quality.get("sites") or {}).items():
            if details.get("status") != "failed":
                continue
            report.issues.append(
                HealthIssue(
                    "warning" if site in fresh_verified_catalogs else "critical",
                    "degraded_site_failed",
                    f"Сайт {site} не дал пригодного результата в прогоне #{last_run.id}.",
                    context={
                        "site": site,
                        "run_id": last_run.id,
                        "reasons": details.get("reasons") or [],
                        "fresh_verified_catalog": site in fresh_verified_catalogs,
                    },
                )
            )

    unverified_sites: dict[str, dict] = {}
    if not full_attempts:
        unverified_sites = {
            site: {"run_id": None, "status": "missing"} for site in storage.FULL_CATALOG_SITES
        }
    else:
        for site in storage.FULL_CATALOG_SITES:
            attempt = full_attempts.get(site)
            if attempt is None:
                unverified_sites[site] = {"run_id": None, "status": "missing"}
                continue
            quality = attempt.run_quality or {}
            site_status = ((quality.get("sites") or {}).get(site) or {}).get("status")
            verified = (
                attempt.status == "ok"
                and quality.get("full_catalog_verified") is True
                and quality.get("financially_eligible") is True
                and site_status == "ok"
            )
            if not verified:
                prior = fresh_verified_catalogs.get(site)
                fresh_fallback = prior is not None and _run_explicitly_rejected_site(attempt, site)
                unverified_sites[site] = {
                    "run_id": attempt.id,
                    "status": attempt.status,
                    "site_status": site_status,
                    "fresh_verified_catalog": fresh_fallback,
                }
                if fresh_fallback:
                    unverified_sites[site].update(
                        {
                            "last_verified_run_id": prior.id,
                            "last_verified_hours_ago": round(_run_age_hours(prior), 1),
                        }
                    )
    if unverified_sites:
        severity: Severity = (
            "critical"
            if any(
                (row.get("status") == "failed" or row.get("site_status") == "failed")
                and not row.get("fresh_verified_catalog")
                for site, row in unverified_sites.items()
            )
            else "warning"
        )
        summary = ", ".join(
            f"{site}=#{row.get('run_id') or '—'}:{row.get('site_status') or row['status']}"
            for site, row in unverified_sites.items()
        )
        has_fresh_fallback = all(
            row.get("fresh_verified_catalog") for row in unverified_sites.values()
        )
        report.issues.append(
            HealthIssue(
                severity,
                "full_catalog_unverified",
                (
                    "Новое полное обновление не подтверждено, но сохранён "
                    "свежий проверенный каталог: "
                    if has_fresh_fallback
                    else "Полный каталог не подтверждён: "
                )
                + f"{summary}.",
                context={
                    "sites": unverified_sites,
                    "fresh_verified_catalog": has_fresh_fallback,
                },
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

    # Запас до нижней границы каталога pharmonline: отказ сбора по границе виден
    # за недели, и сказать о нём надо до того, как он случится.
    report.issues.extend(_check_pharmonline_catalog_floor(session))

    # 7. Per-site silence — `stale_run` смотрит только на ПОСЛЕДНИЙ run в БД, но
    # один сайт может молчать неделю пока другие отрабатывают. Например aloe-run
    # может быть свежим, а недельный pharmonline/aptekonline timer не обновлялся.
    # Проверяем каждый сайт отдельно.
    report.issues.extend(_check_site_silence(session, max_age_hours))

    # 8. Полный отказ сайта: последний прогон, включавший сайт, собрал РОВНО 0
    # товаров → немедленный сигнал (critical, если нет свежего подтверждённого
    # каталога), не дожидаясь порога site_silent. Раньше это терялось:
    # smoke-test пропускал `current == 0`, а
    # `empty_run` смотрит только на последний прогон в принципе (его маскировал
    # intraday-прогон другого сайта).
    report.issues.extend(_check_site_zero_scrape(session, fresh_verified_catalogs))

    # Совокупный статус
    if any(i.severity == "critical" for i in report.issues):
        report.status = "critical"
    elif any(i.severity == "warning" for i in report.issues):
        report.status = "warning"

    return report


def _run_age_hours(run: Run) -> float:
    observed_at = run.finished_at or run.started_at
    return max(0.0, (utcnow() - observed_at).total_seconds() / 3600)


def _latest_verified_catalogs_by_site(session: Session, storage_module) -> dict[str, Run]:
    """Return each site's latest verified catalog, however old it is.

    This is deliberately based on the financial eligibility lineage, not on
    product `last_seen_at`: a rejected run must never masquerade as a verified
    catalog merely because it reached the scraper before being discarded.
    Callers decide what "still fresh" means for their purpose.
    """
    run_ids = storage_module.latest_financial_run_ids_by_site(
        session,
        storage_module.FULL_CATALOG_SITES,
        tenant_id=1,
    )
    if not run_ids:
        return {}
    runs = {
        run.id: run for run in session.scalars(select(Run).where(Run.id.in_(set(run_ids.values()))))
    }
    return {site: runs[run_id] for site, run_id in run_ids.items() if run_id in runs}


def _run_has_fresh_verified_catalogs(run: Run, catalogs_by_site: dict[str, Run]) -> bool:
    """Whether every site in this failed full pass still has safe prior data."""
    if run.catalog_scope != "full":
        return False
    sites = {site.strip() for site in (run.full_catalog_sites or "").split(",") if site.strip()}
    return bool(sites) and all(
        site in catalogs_by_site and _run_explicitly_rejected_site(run, site) for site in sites
    )


def _run_explicitly_rejected_site(run: Run, site: str) -> bool:
    """Whether a terminal run proves this source pass was rejected, not unknown.

    Do not downgrade old or malformed failed-run records: an absent site-level
    quality envelope does not prove that the prior verified catalog can safely
    represent the failed attempt.
    """
    details = ((run.run_quality or {}).get("sites") or {}).get(site) or {}
    return details.get("status") in {"failed", "degraded"}


def alert_signature(report: HealthReport) -> str:
    """Return a stable identity for the active health incident.

    Messages and continuously changing measurements (for example
    ``hours_silent``) are intentionally excluded so an hourly check does not
    create a new incident every hour. Severity, issue code, direct site/status
    identity and the nested per-site statuses used by
    ``full_catalog_unverified`` are included so a real change is delivered
    immediately.

    Standing warnings are not part of the incident: they have their own keys
    (:func:`standing_alert_keys`), so one appearing or changing never restarts
    the incident, and the incident closing is still reported as a recovery.
    """

    issue_keys: list[dict] = []
    for issue in report.issues:
        if issue.severity not in ("warning", "critical") or issue.is_standing:
            continue

        context: dict = {}
        for key in ("site", "status", "site_status", "catalog_scope"):
            value = issue.context.get(key)
            if value is not None:
                context[key] = value

        nested_sites = issue.context.get("sites")
        if isinstance(nested_sites, dict):
            normalized_sites: dict[str, dict | str | int | float | bool | None] = {}
            for site, details in sorted(nested_sites.items(), key=lambda row: str(row[0])):
                if isinstance(details, dict):
                    stable_details = {
                        key: details[key]
                        for key in ("status", "site_status")
                        if details.get(key) is not None
                    }
                    normalized_sites[str(site)] = stable_details
                elif details is None or isinstance(details, (str, int, float, bool)):
                    normalized_sites[str(site)] = details
                else:
                    normalized_sites[str(site)] = str(details)
            context["sites"] = normalized_sites

        issue_keys.append(
            {
                "severity": issue.severity,
                "code": issue.code,
                "context": context,
            }
        )

    issue_keys.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return json.dumps(issue_keys, sort_keys=True, separators=(",", ":"))


def standing_alert_keys(report: HealthReport) -> tuple[str, ...]:
    """Ключи долгих предупреждений отчёта: код, сайт и ступень.

    Ступень (`context["stage"]`) входит в ключ, чтобы переход на следующую
    давал письмо сразу. Сайт и ступень — из короткого постоянного набора
    значений: число или дата в любом из них сделали бы каждый замер новым
    предупреждением, то есть письмом на каждую проверку.
    """
    return tuple(
        sorted(
            {
                "|".join(
                    (
                        issue.code,
                        str(issue.context.get("site") or ""),
                        str(issue.context.get("stage") or ""),
                    )
                )
                for issue in report.issues
                if issue.is_standing
            }
        )
    )


def _standing_notice_due(
    keys: tuple[str, ...],
    last_state: dict | None,
    *,
    now: datetime,
    reminder_hours: float,
) -> bool:
    """Пора ли писать о долгих предупреждениях.

    Да, если хотя бы об одном ещё не писали или писали раньше, чем
    ``reminder_hours`` назад. Время хранится по ключу, поэтому предупреждение,
    которое пропало и вернулось (замер колеблется у порога), второго письма до
    срока не даёт. Нечитаемая запись — писать: лишнее письмо лучше потерянного.
    """
    if not keys:
        return False
    sent = last_state.get("standing") if isinstance(last_state, dict) else None
    if not isinstance(sent, dict):
        return True
    for key in keys:
        try:
            elapsed = now - datetime.fromisoformat(sent[key])
        except (KeyError, TypeError, ValueError):
            return True
        if elapsed < timedelta(0) or elapsed >= timedelta(hours=reminder_hours):
            return True
    return False


def _standing_entry_is_recent(sent_at: object, now: datetime) -> bool:
    if not isinstance(sent_at, str):
        return False
    try:
        elapsed = now - datetime.fromisoformat(sent_at)
    except (TypeError, ValueError):
        return False
    return elapsed < timedelta(hours=4 * STANDING_REMINDER_HOURS)


def _health_state_is_active(last_state: dict | None) -> bool:
    if not isinstance(last_state, dict):
        return False
    signature = last_state.get("signature")
    if last_state.get("version") == 2:
        return (
            last_state.get("status") == "active"
            and isinstance(signature, str)
            and bool(signature.strip())
            and isinstance(last_state.get("last_sent_at"), str)
        )
    # Backward compatibility with a validated v1 {signature, sent_at} state.
    return (
        "version" not in last_state
        and "status" not in last_state
        and isinstance(signature, str)
        and bool(signature.strip())
        and isinstance(last_state.get("sent_at"), str)
    )


def _legacy_alert_signature(report: HealthReport) -> str:
    """Reproduce the v1 ``code:site`` signature for a no-spam migration."""

    return "|".join(
        sorted(
            f"{issue.code}:{issue.context.get('site', '')}"
            for issue in report.issues
            if issue.severity in ("warning", "critical") and not issue.is_standing
        )
    )


def _health_signature_matches(
    report: HealthReport,
    signature: str,
    last_state: dict | None,
) -> bool:
    if not isinstance(last_state, dict):
        return False
    if last_state.get("version") == 2:
        return signature == last_state.get("signature")
    is_legacy = "version" not in last_state and "status" not in last_state
    active_issues = [issue for issue in report.issues if issue.severity in ("warning", "critical")]
    # V1 did not record severity or nested affected-site statuses. Reuse its
    # cooldown only when that lost identity cannot hide an escalation/change;
    # otherwise send once immediately and upgrade the state to v2.
    legacy_identity_is_safe = bool(active_issues) and all(
        issue.severity == "warning" and "sites" not in issue.context for issue in active_issues
    )
    return (
        is_legacy
        and legacy_identity_is_safe
        and last_state.get("signature") == _legacy_alert_signature(report)
    )


def health_alert_decision(
    report: HealthReport,
    last_state: dict | None,
    *,
    now: datetime,
    reminder_hours: float,
    standing_reminder_hours: float = STANDING_REMINDER_HOURS,
) -> HealthAlertDecision:
    """Choose the next email transition for the current health report.

    - a new or changed incident is sent immediately;
    - an unchanged incident is reminded after ``reminder_hours``;
    - the first healthy check after an active incident sends one recovery;
    - a healthy state stays quiet until a new incident appears.

    Долгие предупреждения (`HealthIssue.standing`) инцидентом не считаются и
    идут второй дорожкой: письмо при появлении и при смене ступени, дальше — не
    чаще раза в ``standing_reminder_hours``. Без этого предупреждение, которое
    висит месяцами, давало бы письмо каждые ``reminder_hours``, а отчёт никогда
    не становился бы «ok» — и о закрытии настоящего инцидента письма не было бы.
    Письмо об инциденте несёт отчёт целиком и засчитывается долгим
    предупреждениям. Письмо о восстановлении — нет: его читают как «делать
    нечего», и новое предупреждение или новая ступень, впервые показанные под
    темой RECOVERED, остались бы незамеченными на неделю. О них уйдёт отдельное
    письмо со следующей проверкой.

    Missing or malformed incident timestamps are fail-open: the active alert is
    sent again rather than silently lost.
    """

    previous_signature = last_state.get("signature") if isinstance(last_state, dict) else None
    was_active = _health_state_is_active(last_state)
    standing_keys = standing_alert_keys(report)
    standing_due = _standing_notice_due(
        standing_keys,
        last_state,
        now=now,
        reminder_hours=standing_reminder_hours,
    )

    if not report.has_incident:
        if was_active:
            return HealthAlertDecision(
                "recovery",
                previous_signature=previous_signature,
            )
        if standing_due:
            return HealthAlertDecision("notice", standing_keys=standing_keys)
        return HealthAlertDecision(None)

    signature = alert_signature(report)
    if not was_active or not _health_signature_matches(report, signature, last_state):
        return HealthAlertDecision(
            "incident",
            signature=signature,
            previous_signature=previous_signature,
            standing_keys=standing_keys,
        )

    sent_at = None
    if isinstance(last_state, dict):
        sent_at = last_state.get("last_sent_at") or last_state.get("sent_at")
    try:
        last_sent_at = datetime.fromisoformat(sent_at)
        elapsed = now - last_sent_at
        # A future timestamp usually means clock skew or a malformed state.
        # Fail open so an active incident is not suppressed indefinitely.
        reminder_due = elapsed < timedelta(0) or elapsed >= timedelta(hours=reminder_hours)
    except (TypeError, ValueError):
        reminder_due = True

    # Долгое предупреждение, о котором пора написать, едет в письме об
    # инциденте: отчёт в нём тот же, а отдельного действия для активного
    # инцидента заводить незачем.
    if reminder_due or standing_due:
        return HealthAlertDecision(
            "reminder",
            signature=signature,
            previous_signature=previous_signature,
            standing_keys=standing_keys,
        )
    return HealthAlertDecision(None, signature=signature, previous_signature=previous_signature)


def health_alert_state_after(
    decision: HealthAlertDecision,
    last_state: dict | None,
    *,
    now: datetime,
) -> dict:
    """Build the state persisted after a transition email was sent."""

    sent_at = now.isoformat()
    if decision.action == "recovery":
        state = {
            "version": 2,
            "status": "ok",
            "signature": None,
            "recovered_signature": decision.previous_signature,
            "recovered_at": sent_at,
            "last_sent_at": sent_at,
        }
    elif decision.action == "notice":
        # Инцидента нет и не было: меняется только дорожка долгих предупреждений.
        state = {
            "version": 2,
            "status": "ok",
            "signature": None,
            "last_sent_at": sent_at,
        }
        if isinstance(last_state, dict) and last_state.get("status") == "ok":
            for key in ("recovered_signature", "recovered_at"):
                if key in last_state:
                    state[key] = last_state[key]
    elif decision.action in ("incident", "reminder") and decision.signature:
        incident_started_at = sent_at
        if decision.action == "reminder" and isinstance(last_state, dict):
            incident_started_at = last_state.get("incident_started_at") or (
                last_state.get("sent_at") or sent_at
            )
        state = {
            "version": 2,
            "status": "active",
            "signature": decision.signature,
            "incident_started_at": incident_started_at,
            "last_sent_at": sent_at,
        }
    else:
        raise ValueError("cannot persist a health state without an email transition")

    # Время отправки — по каждому ключу. Прежние записи остаются: по ним
    # предупреждение, которое пропало и вернулось, не шлётся заново до срока.
    # Записи старше нескольких сроков напоминания уже ни на что не влияют и
    # выбрасываются, чтобы файл не рос.
    previous = last_state.get("standing") if isinstance(last_state, dict) else None
    standing = {
        key: value
        for key, value in (previous.items() if isinstance(previous, dict) else ())
        if isinstance(key, str) and _standing_entry_is_recent(value, now)
    }
    standing.update(dict.fromkeys(decision.standing_keys, sent_at))
    if standing:
        state["standing"] = standing
    return state


def alert_due(
    signature: str,
    last_state: dict | None,
    *,
    now: datetime,
    cooldown_hours: float,
) -> bool:
    """Backward-compatible low-level cooldown helper.

    New health-email code should use :func:`health_alert_decision` so recovery
    transitions are represented as well.
    """
    if not _health_state_is_active(last_state) or last_state.get("signature") != signature:
        return True
    sent_at = last_state.get("last_sent_at") or last_state.get("sent_at")
    if not sent_at:
        return True
    try:
        last_dt = datetime.fromisoformat(sent_at)
        elapsed = now - last_dt
    except (TypeError, ValueError):
        return True
    return elapsed < timedelta(0) or elapsed >= timedelta(hours=cooldown_hours)


# Окно СВЕЖЕСТИ для знаменателя site_drop: живым считается каталог, виденный за
# один цикл сбора + 2 дня запаса (суточный сайт → 3д, недельный → 9д). Раньше
# тут стояла константа 3 для всех — у недельного сайта она обнуляла знаменатель
# уже на четвёртый день, и проверка покрытия молча отключалась.
_SITE_FRESHNESS_DAYS: dict[str, int] = {
    site: site_cadence_days(site) + 2 for site in SITE_SCRAPE_CADENCE_HOURS
}
_DEFAULT_FRESHNESS_DAYS = 3

# Окно ПОКРЫТИЯ для числителя site_drop: сколько РАЗНЫХ товаров сайта видели за
# последние N дней (≥ один полный цикл скрейпа + запас). Берём окно, а НЕ
# буквальный последний прогон — последним бывает ЧАСТИЧНЫЙ intraday-прогон
# (pharmonline run_277 = 127 товаров featured-выборки), тогда seen=127/каталог
# дал бы ложный site_drop. Окно включает последний ПОЛНЫЙ прогон → одиночный
# частичный прогон метрику не роняет. Должно быть < _SITE_FRESHNESS_DAYS.
# Один цикл сбора + 1 день запаса (суточный → 2д, недельный → 8д) — окно всегда
# захватывает последний ПОЛНЫЙ прогон и всегда уже окна свежести.
_SITE_COVERAGE_DAYS: dict[str, int] = {
    site: site_cadence_days(site) + 1 for site in SITE_SCRAPE_CADENCE_HOURS
}
_DEFAULT_COVERAGE_DAYS = 2


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
    из-за частичного run_277. Окно покрытия (2д) + свежести (3д) → ~99%.
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


# Per-site пороги «молчания» (часы) = ритм сбора + запас (src/cadence.py):
# суточный сайт → 30ч, недельный → 174ч. Расписание описано в коде, а не только
# в systemd-override, чтобы health падал закрыто, когда ритм прервался.
_SITE_MAX_AGE_HOURS: dict[str, int] = {
    site: site_max_age_hours(site) for site in SITE_SCRAPE_CADENCE_HOURS
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


def _check_site_zero_scrape(
    session: Session,
    fresh_verified_catalogs: dict[str, Run] | None = None,
) -> list[HealthIssue]:
    """Report a zero scrape, preserving the severity of a real stale outage.

    Ловит полный отказ сайта (лёг / прокси умер / сменилась вёрстка) в течение
    часа (hourly health-check + --alert-email), не дожидаясь порога site_silent
    (~сутки). Реальный кейс (2026-06-11): aloe.az отдал HTTP 502 → прогон собрал
    0 товаров, но не алертнул — `_smoke_test_per_site_coverage` пропускал
    `current == 0` (самый худший случай!), а `empty_run` маскировался intraday.

    Если сохранён свежий финансово подтверждённый каталог этого сайта, сигнал
    остаётся warning: данные клиента не пропали, но обновление требует внимания.

    Порог именно ==0: intraday-блипы (частичные ~100-250 шт) НЕ триггерят. Берём
    последний прогон С ЭТИМ сайтом в `products_per_site`, поэтому intraday-прогон
    другого сайта (pharmonline featured) не маскирует 0 у aloe/aptek.
    """
    sites = {"pharmonline", "aptekonline", "aloe"}
    fresh_verified_catalogs = fresh_verified_catalogs or {}
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
                    prior = fresh_verified_catalogs.get(site)
                    fresh_fallback = prior is not None and _run_explicitly_rejected_site(run, site)
                    issues.append(
                        HealthIssue(
                            "warning" if fresh_fallback else "critical",
                            "site_zero_scrape",
                            (
                                f"Сайт {site}: последний прогон #{run.id} собрал 0 товаров, "
                                f"но сохранён свежий проверенный каталог #{prior.id}."
                                if fresh_fallback
                                else f"Сайт {site}: последний прогон #{run.id} собрал 0 товаров "
                                "(сайт лёг / прокси упал / сменилась вёрстка?)."
                            ),
                            context={
                                "site": site,
                                "run_id": run.id,
                                "fresh_verified_catalog": fresh_fallback,
                                "last_verified_run_id": prior.id if fresh_fallback else None,
                            },
                        )
                    )
        if seen == sites:
            break
    return issues


# Нижняя граница каталога pharmonline. Полный сбор в режиме public_api
# отказывает, когда товаров в API меньше границы из
# `pharmonline_public_api_catalog_baselines` (счётчик `catalog_floor_failed` в
# `main._verify_pharmonline_public_api_identities`), и ниже границы отказывает
# же ручная сверка — его штатный путь восстановления. Сама граница не
# опускается, а каталог убывает, так что отказ виден заранее.
#
# Пороги — в товарах, а не в неделях. Убыль идёт рывками (замер 2026-10-08: с
# 6 сентября по 4 октября −3 товара, затем по 6 в день; со 2 на 3 сентября за
# сутки ушло от 82 до 102), и срок, посчитанный по среднему темпу, ошибается в
# разы. Первый порог — около двух-трёх месяцев при 3–6 товарах в день, второй —
# одна такая разовая чистка каталога.
_PHARMONLINE_FLOOR_MARGIN_LOW = 300
_PHARMONLINE_FLOOR_MARGIN_VERY_LOW = 100

# С этого начинается причина отказа, когда каталог прочитан целиком, а не
# прошла только сверка личностей (её пишет `run_cmd` в src/main.py). Такое
# чтение уже прошло и полноту маршрута, и сверку с sitemap, и порог 0.90 от
# прошлых прогонов, поэтому его размер годится для сравнения с границей.
_PUBLIC_API_IDENTITY_REFUSAL_PREFIX = "public_api_identity_proof_failed:"

# Сколько последних полных прогонов pharmonline просматривать в поисках такого
# чтения. Месяц ежедневных сбоев до сверки (прокси, сентябрь 2026) — 30 строк.
_PHARMONLINE_FLOOR_SCAN_RUNS = 200


def _pharmonline_catalog_floor(session: Session, *, tenant_id: int = 1) -> int | None:
    """Действующая нижняя граница каталога или None, если её нет.

    Берётся так же, как в сборе: наибольшее значение среди строк тенанта.
    """
    from sqlalchemy import inspect

    from src.storage import PharmonlinePublicAPICatalogBaseline as Baseline

    # Через соединение сессии, а не через Engine: см. комментарий в
    # main._pharmonline_public_api_identity_tables_available.
    if Baseline.__table__.name not in set(inspect(session.connection()).get_table_names()):
        return None
    floor = session.scalar(
        select(func.max(Baseline.minimum_catalog_item_count)).where(Baseline.tenant_id == tenant_id)
    )
    return int(floor) if floor else None


def _latest_complete_pharmonline_catalog_read(
    session: Session,
    *,
    tenant_id: int = 1,
) -> tuple[int, datetime, int] | None:
    """Последний полный сбор pharmonline, прочитавший каталог API целиком.

    Возвращает номер прогона, время его конца и число товаров. Годится и сбор,
    которому отказала только сверка личностей: он считает те же товары, что
    сравниваются с границей, а подтверждённые сборы бывают реже (до автодопуска
    новых товаров сбор отказывал неделями). Сбор, остановленный раньше — сбой
    сайта, неполный маршрут, меньше 0.90 прошлого каталога, — размером каталога
    не считается.
    """
    rows = session.execute(
        select(
            Run.id,
            Run.finished_at,
            Run.run_quality,
            Run.catalog_verification_reason,
        )
        .where(
            Run.tenant_id == tenant_id,
            Run.status != "running",
            Run.finished_at.is_not(None),
            Run.catalog_scope == "full",
            Run.full_catalog_sites.contains("pharmonline"),
        )
        .order_by(desc(Run.finished_at), desc(Run.id))
        .limit(_PHARMONLINE_FLOOR_SCAN_RUNS)
    )
    for run_id, finished_at, run_quality, column_reason in rows:
        # Проверка справочная: строка непривычной формы пропускается, а не роняет
        # весь health вместе с письмами о настоящих поломках.
        quality = run_quality if isinstance(run_quality, dict) else {}
        if quality.get("mode") != "public_api":
            continue
        sites = quality.get("sites")
        details = sites.get("pharmonline") if isinstance(sites, dict) else None
        if not isinstance(details, dict):
            continue
        products = details.get("products")
        if details.get("status") != "ok" or type(products) is not int or products < 1:
            continue
        # Отметку «каталог подтверждён» сбор ставит после всех проверок чтения и
        # снимает, если отказала сверка личностей, — тогда остаётся причина.
        reason = str(quality.get("catalog_verification_reason") or column_reason or "")
        if quality.get("full_catalog_verified") is True or reason.startswith(
            _PUBLIC_API_IDENTITY_REFUSAL_PREFIX
        ):
            return run_id, finished_at, products
    return None


def _check_pharmonline_catalog_floor(session: Session) -> list[HealthIssue]:
    """Сколько товаров осталось до нижней границы каталога pharmonline.

    Малый запас — долгое предупреждение двух ступеней; каталог ниже границы —
    уже поломка: сбор отказывает и сам не восстановится. Нет границы или нет
    ни одного целиком прочитанного каталога — сравнивать нечего.
    """
    floor = _pharmonline_catalog_floor(session)
    if floor is None:
        return []
    observed = _latest_complete_pharmonline_catalog_read(session)
    if observed is None:
        return []
    run_id, finished_at, products = observed
    margin = products - floor
    if margin > _PHARMONLINE_FLOOR_MARGIN_LOW:
        return []

    read = f"сбор #{run_id} от {finished_at:%Y-%m-%d}"
    runbook = "docs/RUNBOOK.md, «Нижняя граница каталога pharmonline»"
    context = {
        "site": "pharmonline",
        "run_id": run_id,
        "products": products,
        "floor": floor,
        "margin": margin,
    }
    # Сбор отказывает при «меньше границы»: ровно на границе он ещё проходит.
    if margin < 0:
        return [
            HealthIssue(
                "critical",
                "pharmonline_catalog_floor",
                f"pharmonline: каталог ниже нижней границы — {products} при границе {floor} "
                f"({read}). Плановый сбор и ручная сверка отказывают, пока границу не "
                f"опустят: {runbook}.",
                context={**context, "stage": "below"},
            )
        ]
    return [
        HealthIssue(
            "warning",
            "pharmonline_catalog_floor",
            f"pharmonline: запас до нижней границы каталога — {margin} "
            f"(товаров {products}, граница {floor}, {read}). Ниже границы откажут и "
            f"плановый сбор, и ручная сверка, а сама граница не опускается: {runbook}.",
            context={
                **context,
                "stage": "very_low" if margin <= _PHARMONLINE_FLOOR_MARGIN_VERY_LOW else "low",
            },
            standing=True,
        )
    ]


def render_alert_html(report: HealthReport, *, recovered: bool = False) -> str:
    """Простое HTML-письмо для алерта.

    ``recovered`` — письмо о закрытии инцидента. Если при этом остаются долгие
    предупреждения, отчёт не «ok», и без флажка письмо с темой RECOVERED
    открывалось бы заголовком «Внимание».
    """
    color = {"ok": "#34c759", "warning": "#ff9500", "critical": "#ff3b30"}[report.status]
    title = {"ok": "✓ OK", "warning": "⚠️ Внимание", "critical": "🔴 Проблема"}[report.status]
    if recovered and report.issues and not report.has_incident:
        color = "#34c759"
        title = "✓ Инцидент закрыт"

    rows = []
    for i in report.issues:
        sev_color = {"warning": "#ff9500", "critical": "#ff3b30", "ok": "#34c759"}[i.severity]
        note = (
            "<div style='font-size:11px;color:#86868b;margin-top:4px;'>"
            f"Долгое предупреждение: напоминание раз в {STANDING_REMINDER_HOURS // 24} дней."
            "</div>"
            if i.is_standing
            else ""
        )
        rows.append(
            f"<tr><td style='padding:8px;border-bottom:1px solid #eee;'>"
            f"<span style='color:{sev_color};font-weight:600;'>{i.severity.upper()}</span> "
            f"<code style='font-size:11px;color:#86868b;'>{i.code}</code></td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee;'>{i.message}{note}</td></tr>"
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
