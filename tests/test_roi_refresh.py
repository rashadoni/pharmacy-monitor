"""Пересчёт кэша рекомендаций без сбора: `src/roi_refresh.py`, CLI и эндпоинты.

Главное, что здесь закреплено: после смены порогов и после правки пары кэш
отдаёт рекомендации, посчитанные по новым данным, и для этого не создаётся ни
одного нового Run.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from src import api as api_module
from src import main as main_mod
from src import roi, roi_refresh, storage, tenants
from src._time import utcnow
from src.cadence import site_max_age_hours
from src.storage import (
    Match,
    MatchRejection,
    PriceSnapshot,
    Product,
    RoiActionsCache,
    RoiRefreshRequest,
    Run,
)

SITES = ("pharmonline", "aptekonline", "aloe")


def _full_run(s, sites=SITES, *, status="ok", finished_at=None) -> Run:
    """Полный прогон: подтверждённый при status='ok', иначе проваленный."""
    verified = status == "ok"
    moment = finished_at or utcnow()
    run = Run(
        tenant_id=1,
        started_at=moment,
        finished_at=moment,
        status=status,
        catalog_scope="full",
        full_catalog_sites=",".join(sites),
        catalog_verified=verified,
        run_quality={
            "version": 1,
            "baseline_enforced": True,
            "full_catalog_verified": verified,
            "financially_eligible": verified,
            "sites": {site: {"status": "ok" if verified else "failed"} for site in sites},
        },
    )
    s.add(run)
    s.flush()
    return run


def _cluster(s, run: Run, name: str, prices: dict[str, float]) -> Match:
    match = Match(tenant_id=1, canonical_name=name, confidence=1.0)
    s.add(match)
    s.flush()
    for site, price in prices.items():
        product = Product(
            tenant_id=1,
            site=site,
            external_id=f"{site}-{name}",
            url=f"http://{site}.az/p/{name}",
            name=name,
            name_normalized=name.lower(),
            canonical_id=match.id,
        )
        s.add(product)
        s.flush()
        s.add(PriceSnapshot(run_id=run.id, product_id=product.id, price=price))
    s.commit()
    return match


def _run_count(s) -> int:
    return s.scalar(select(func.count(Run.id)))


def _requests(s) -> list[RoiRefreshRequest]:
    s.expire_all()
    return list(s.scalars(select(RoiRefreshRequest).order_by(RoiRefreshRequest.id)).all())


def _queue(s, reason="test") -> None:
    roi_refresh.request_refresh(s, tenant_id=1, reason=reason)
    s.commit()


def _cached_types(s, name: str, site: str = "pharmonline") -> set[str] | None:
    cached = roi.get_cached_actions(s, site)
    if cached is None:
        return None
    return {item["type"] for item in cached if item["product_name"] == name}


# ─── Пересчёт от подтверждённой эпохи ────────────────────────────────────────


def test_refresh_writes_cache_for_every_site_without_creating_a_run(db_session):
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    runs_before = _run_count(db_session)

    result = roi_refresh.refresh_from_trusted_epoch(db_session)

    assert result.outcome == "refreshed"
    assert result.run_id == run.id
    assert set(result.counts) == set(SITES)
    assert _run_count(db_session) == runs_before
    rows = db_session.scalars(select(RoiActionsCache)).all()
    assert {row.client_site for row in rows} == set(SITES)
    assert {row.run_id for row in rows} == {run.id}
    assert _cached_types(db_session, "Aspirin") == {"price_raise"}


def test_cache_is_signed_by_the_latest_of_the_per_site_full_runs(db_session):
    """Сайты собираются разными прогонами: кэш подписывает самый поздний.

    Подпиши его любой другой — читатель сочтёт кэш вытесненным более поздним
    прогоном и не отдаст.
    """
    now = utcnow()
    first = _full_run(db_session, ("pharmonline",), finished_at=now - timedelta(hours=3))
    _full_run(db_session, ("aptekonline",), finished_at=now - timedelta(hours=2))
    latest = _full_run(db_session, ("aloe",), finished_at=now - timedelta(hours=1))
    _cluster(db_session, first, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})

    result = roi_refresh.refresh_from_trusted_epoch(db_session)

    assert result.outcome == "refreshed"
    assert result.run_id == latest.id
    snapshot = roi.get_cached_actions_snapshot(db_session, "pharmonline")
    assert snapshot is not None
    assert snapshot[2].id == latest.id


def test_refresh_refuses_when_a_site_has_no_verified_catalog(db_session):
    """Состояние прода 2026-10-07: последний полный сбор pharmonline упал."""
    now = utcnow()
    verified = _full_run(db_session, SITES, finished_at=now - timedelta(hours=2))
    _cluster(db_session, verified, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0})
    failed = _full_run(
        db_session, ("pharmonline",), status="failed", finished_at=now - timedelta(hours=1)
    )
    db_session.commit()
    _queue(db_session)

    result = roi_refresh.run_refresh(db_session, only_if_requested=True)

    assert result.outcome == "untrusted"
    assert f"pharmonline:run={failed.id},status=failed" in result.reason
    assert db_session.scalars(select(RoiActionsCache)).all() == []
    (request,) = _requests(db_session)
    # Повтор ничего не даст: доверие вернёт только сбор, а он пересчитает сам.
    assert request.status == "skipped"
    assert request.detail == result.reason


def test_refresh_refuses_when_no_full_scan_was_ever_made(db_session):
    result = roi_refresh.refresh_from_trusted_epoch(db_session)

    assert result.outcome == "untrusted"
    assert "pharmonline:no_full_scan" in result.reason


def test_refresh_refuses_stale_catalog(db_session):
    old = utcnow() - timedelta(hours=max(site_max_age_hours(site) for site in SITES) + 1)
    run = _full_run(db_session, finished_at=old)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0})

    result = roi_refresh.refresh_from_trusted_epoch(db_session)

    assert result.outcome == "untrusted"
    assert db_session.scalars(select(RoiActionsCache)).all() == []


def test_closed_policy_gate_does_not_cache_an_empty_list(db_session, monkeypatch):
    """Закрытый гейт политики — это «не знаем», а не «рекомендаций нет»."""
    from src import product_policy

    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    monkeypatch.setattr(
        product_policy,
        "policy_rollout_eligibility",
        lambda *args, **kwargs: product_policy.Eligibility(False, "full_catalog_trust_not_ready"),
    )

    result = roi_refresh.refresh_from_trusted_epoch(db_session)

    assert result.outcome == "untrusted"
    assert result.reason == "policy_gate:full_catalog_trust_not_ready"
    assert db_session.scalars(select(RoiActionsCache)).all() == []


# ─── Блокировка сборов ───────────────────────────────────────────────────────


def test_refresh_is_deferred_while_a_scrape_holds_the_lock(db_session, monkeypatch):
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    roi_refresh.refresh_from_trusted_epoch(db_session)
    before = db_session.scalar(select(RoiActionsCache.computed_at).limit(1))
    _queue(db_session)

    @contextmanager
    def scrape_running(_session):
        yield False

    monkeypatch.setattr(roi_refresh, "try_shared_scrape_read_lock", scrape_running)
    monkeypatch.setattr(
        roi,
        "refresh_all_cached_actions",
        lambda *args, **kwargs: pytest.fail("catalog must not be read during a scrape"),
    )

    result = roi_refresh.run_refresh(db_session, only_if_requested=True)

    assert result.outcome == "busy"
    assert result.reason == "scrape_in_progress"
    assert [request.status for request in _requests(db_session)] == ["pending"]
    assert db_session.scalar(select(RoiActionsCache.computed_at).limit(1)) == before


def test_unfinished_run_defers_instead_of_caching_an_empty_list(db_session):
    """`_compute_actions_locked` при незавершённом прогоне молча отдаёт [].

    Запиши пересчёт этот список в кэш — дашборд показал бы «рекомендаций нет»
    вместо настоящих.
    """
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    roi_refresh.refresh_from_trusted_epoch(db_session)
    db_session.add(Run(tenant_id=1, started_at=utcnow(), finished_at=None, status="running"))
    db_session.commit()
    _queue(db_session)

    result = roi_refresh.run_refresh(db_session, only_if_requested=True)

    assert result.outcome == "busy"
    assert result.reason == "run_unfinished"
    assert [request.status for request in _requests(db_session)] == ["pending"]
    payload = db_session.scalar(
        select(RoiActionsCache.payload).where(RoiActionsCache.client_site == "pharmonline")
    )
    assert [item["type"] for item in payload] == ["price_raise"]


def test_second_refresh_does_not_start_while_one_is_running(db_session, monkeypatch):
    @contextmanager
    def taken(_session):
        yield False

    monkeypatch.setattr(roi_refresh, "try_exclusive_roi_refresh_lock", taken)

    result = roi_refresh.refresh_from_trusted_epoch(db_session)

    assert result.outcome == "busy"
    assert result.reason == "another_refresh_running"


# ─── Очередь ─────────────────────────────────────────────────────────────────


def test_watcher_mode_does_nothing_without_requests(db_session, monkeypatch):
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    monkeypatch.setattr(
        roi_refresh,
        "refresh_from_trusted_epoch",
        lambda *args, **kwargs: pytest.fail("nothing was requested"),
    )

    assert roi_refresh.run_refresh(db_session, only_if_requested=True).outcome == "idle"


def test_one_refresh_closes_every_request_visible_at_its_start(db_session):
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    for reason in ("match_reject", "match_relink", "pricing_config"):
        _queue(db_session, reason)

    result = roi_refresh.run_refresh(db_session, only_if_requested=True)

    assert result.outcome == "refreshed"
    assert result.requests_closed == 3
    requests = _requests(db_session)
    assert [request.status for request in requests] == ["done", "done", "done"]
    assert {request.run_id for request in requests} == {run.id}
    assert all(request.completed_at is not None for request in requests)


def test_request_that_appears_during_the_refresh_stays_pending(db_session, monkeypatch):
    """Правка, закоммиченная посреди пересчёта, должна дать ещё один пересчёт.

    Вторая заявка намеренно имеет МЕНЬШИЙ id, чем закрываемая: id выдаётся при
    вставке, а видимой заявка становится при коммите. Закрытие «всех с id не
    больше максимального на старте» потеряло бы её.
    """
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    late = RoiRefreshRequest(tenant_id=1, reason="late_commit", status="done")
    db_session.add(late)
    db_session.commit()
    _queue(db_session, "seen_at_start")
    real_refresh = roi.refresh_all_cached_actions

    def refresh_with_concurrent_edit(session, **kwargs):
        counts = real_refresh(session, **kwargs)
        session.get(RoiRefreshRequest, late.id).status = "pending"
        session.add(RoiRefreshRequest(tenant_id=1, reason="new_edit", status="pending"))
        session.commit()
        return counts

    monkeypatch.setattr(roi, "refresh_all_cached_actions", refresh_with_concurrent_edit)

    result = roi_refresh.run_refresh(db_session, only_if_requested=True)

    assert result.requests_closed == 1
    assert {request.reason: request.status for request in _requests(db_session)} == {
        "late_commit": "pending",
        "seen_at_start": "done",
        "new_edit": "pending",
    }


def test_thresholds_changed_mid_refresh_do_not_leave_old_recommendations(db_session, monkeypatch):
    """Эндпоинт сбросил кэш, а шедший расчёт дописал срезы по старым порогам."""
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    _queue(db_session, "match_reject")
    real_refresh = roi.refresh_all_cached_actions

    def refresh_while_admin_saves_thresholds(session, **kwargs):
        counts = real_refresh(session, **kwargs)
        roi_refresh.drop_cache_and_request_refresh(
            session, tenant_id=1, reason=roi_refresh.REASON_PRICING_CONFIG
        )
        session.commit()
        # Последний срез расчёт дописывает уже после сброса.
        real_refresh(session, **kwargs)
        return counts

    monkeypatch.setattr(roi, "refresh_all_cached_actions", refresh_while_admin_saves_thresholds)

    assert roi_refresh.run_refresh(db_session, only_if_requested=True).outcome == "refreshed"

    assert db_session.scalars(select(RoiActionsCache)).all() == []
    assert {r.reason: r.status for r in _requests(db_session)} == {
        "match_reject": "done",
        "pricing_config": "pending",
    }
    monkeypatch.setattr(roi, "refresh_all_cached_actions", real_refresh)
    assert roi_refresh.run_refresh(db_session, only_if_requested=True).outcome == "refreshed"
    assert _cached_types(db_session, "Aspirin") == {"price_raise"}


def test_match_edit_mid_refresh_keeps_the_previous_list_visible(db_session, monkeypatch):
    """Правка пары кэш не сбрасывает — и посреди расчёта тоже."""
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    _queue(db_session, "match_reject")
    real_refresh = roi.refresh_all_cached_actions

    def refresh_with_another_edit(session, **kwargs):
        counts = real_refresh(session, **kwargs)
        roi_refresh.request_refresh(session, tenant_id=1, reason="match_relink")
        session.commit()
        return counts

    monkeypatch.setattr(roi, "refresh_all_cached_actions", refresh_with_another_edit)

    roi_refresh.run_refresh(db_session, only_if_requested=True)

    assert len(db_session.scalars(select(RoiActionsCache)).all()) == 3
    assert roi_refresh.has_pending_request(db_session, tenant_id=1)


def test_only_declared_reasons_may_drop_the_cache(db_session):
    with pytest.raises(ValueError, match="cache-dropping"):
        roi_refresh.drop_cache_and_request_refresh(db_session, tenant_id=1, reason="match_reject")


def test_failure_before_the_calculation_leaves_cache_and_request_alone(db_session, monkeypatch):
    """Не открылось соединение под блокировку: ничего не считали — нечего и стирать."""
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    roi_refresh.refresh_from_trusted_epoch(db_session)
    _queue(db_session, "match_reject")

    @contextmanager
    def no_connection(_session):
        raise ConnectionError("pool exhausted")
        yield  # pragma: no cover

    monkeypatch.setattr(roi_refresh, "try_shared_scrape_read_lock", no_connection)

    with pytest.raises(ConnectionError):
        roi_refresh.run_refresh(db_session, only_if_requested=True)

    assert len(db_session.scalars(select(RoiActionsCache)).all()) == 3
    assert [(r.reason, r.status) for r in _requests(db_session)] == [("match_reject", "pending")]


def test_failed_slice_closes_requests_as_failed(db_session, monkeypatch):
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    _queue(db_session)
    real_compute = roi._compute_actions_locked

    def compute(session, *, client_site=None, **kwargs):
        if client_site == "aloe":
            raise RuntimeError("boom")
        return real_compute(session, client_site=client_site, **kwargs)

    monkeypatch.setattr(roi, "_compute_actions_locked", compute)

    result = roi_refresh.run_refresh(db_session, only_if_requested=True)

    assert result.outcome == "failed"
    assert result.reason == "sites:aloe"
    request, retry = _requests(db_session)
    assert (request.status, request.detail) == ("failed", "sites:aloe")
    # Упавший срез не оставляет старый ответ, остальные посчитаны.
    assert roi.get_cached_actions(db_session, "aloe") is None
    assert roi.get_cached_actions(db_session, "pharmonline") is not None

    # Сбой мог быть разовым — пересчёт ставит себе один повтор…
    assert (retry.reason, retry.status) == ("retry_after_failure", "pending")
    assert roi_refresh.run_refresh(db_session, only_if_requested=True).outcome == "failed"
    # …и только один: устойчивая поломка не превращается в падение каждую минуту.
    assert [(r.reason, r.status) for r in _requests(db_session)] == [
        ("test", "failed"),
        ("retry_after_failure", "failed"),
    ]
    assert roi_refresh.run_refresh(db_session, only_if_requested=True).outcome == "idle"


def test_retry_after_a_passing_failure_restores_the_cache(db_session, monkeypatch):
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    _queue(db_session)
    real_refresh = roi.refresh_all_cached_actions
    monkeypatch.setattr(
        roi,
        "refresh_all_cached_actions",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("database went away")),
    )

    crashed = roi_refresh.run_refresh(db_session, only_if_requested=True)

    assert (crashed.outcome, crashed.reason) == ("failed", "error:ConnectionError")
    monkeypatch.setattr(roi, "refresh_all_cached_actions", real_refresh)
    assert roi_refresh.run_refresh(db_session, only_if_requested=True).outcome == "refreshed"
    assert _cached_types(db_session, "Aspirin") == {"price_raise"}
    assert [(r.reason, r.status) for r in _requests(db_session)] == [
        ("test", "failed"),
        ("retry_after_failure", "done"),
    ]


def test_crash_mid_refresh_leaves_no_stale_answer_behind(db_session, monkeypatch):
    """Исключение мимо разбора «срез упал»: кэш убран целиком, сессия жива."""
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    roi_refresh.refresh_from_trusted_epoch(db_session)
    assert len(db_session.scalars(select(RoiActionsCache)).all()) == 3
    _queue(db_session, "match_reject")
    monkeypatch.setattr(
        roi,
        "refresh_all_cached_actions",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("transaction aborted")),
    )

    result = roi_refresh.run_refresh(db_session, only_if_requested=True)

    assert (result.outcome, result.reason) == ("failed", "error:RuntimeError")
    assert db_session.scalars(select(RoiActionsCache)).all() == []
    assert [(r.reason, r.status, r.detail) for r in _requests(db_session)] == [
        ("match_reject", "failed", "error:RuntimeError"),
        ("retry_after_failure", "pending", None),
    ]


def test_failed_manual_refresh_does_not_queue_a_retry(db_session, monkeypatch):
    """Ручной запуск сам видит код возврата — повтор за него никто не ставит."""
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    monkeypatch.setattr(
        roi,
        "_compute_actions_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert roi_refresh.run_refresh(db_session).outcome == "failed"

    assert _requests(db_session) == []


def test_manual_refresh_runs_without_requests_and_closes_pending_ones(db_session):
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})

    assert roi_refresh.run_refresh(db_session).outcome == "refreshed"

    _queue(db_session)
    result = roi_refresh.run_refresh(db_session)
    assert (result.outcome, result.requests_closed) == ("refreshed", 1)


def test_old_closed_requests_are_pruned_and_recent_ones_kept(db_session):
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    long_ago = utcnow() - timedelta(days=45)
    db_session.add_all(
        [
            RoiRefreshRequest(tenant_id=1, reason="old_done", status="done", completed_at=long_ago),
            RoiRefreshRequest(
                tenant_id=1,
                reason="recent_done",
                status="done",
                completed_at=utcnow() - timedelta(days=2),
            ),
            RoiRefreshRequest(
                tenant_id=1, reason="old_pending", status="pending", requested_at=long_ago
            ),
        ]
    )
    db_session.commit()

    roi_refresh.run_refresh(db_session, only_if_requested=True)

    assert {request.reason: request.status for request in _requests(db_session)} == {
        "recent_done": "done",
        "old_pending": "done",
    }


def test_requests_of_other_tenants_are_left_alone(db_session):
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    _queue(db_session)
    roi_refresh.request_refresh(db_session, tenant_id=2, reason="other_tenant")
    db_session.commit()

    assert roi_refresh.tenants_with_pending_requests(db_session) == [1, 2]
    roi_refresh.run_refresh(db_session, tenant_id=1, only_if_requested=True)

    assert {request.tenant_id: request.status for request in _requests(db_session)} == {
        1: "done",
        2: "pending",
    }
    assert roi_refresh.has_pending_request(db_session, tenant_id=2)
    assert not roi_refresh.has_pending_request(db_session, tenant_id=1)
    # Закрытая заявка — не повод будить пересчёт этого тенанта на каждом тике.
    assert roi_refresh.tenants_with_pending_requests(db_session) == [2]


# ─── Эндпоинты + CLI: то, что видит пользователь ─────────────────────────────


@pytest.fixture
def api_db(monkeypatch, db_session):
    Session = sessionmaker(db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(storage, "make_session", lambda database_url=None: Session)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setenv("JWT_SECRET", "test-secret-very-long-not-for-prod-only")
    monkeypatch.setenv("PHARMACY_AUTH_DEV_SHOW_TOKEN", "1")
    return db_session


@pytest.fixture
def client(api_db):
    """TestClient под админом тенанта 1."""
    if not api_module._JWT_AVAILABLE:
        pytest.skip("python-jose not installed — JWT tests skipped")
    tenant = tenants.get_or_create_default(api_db)
    user = storage.TenantUser(
        tenant_id=tenant.id,
        email="admin@example.com",
        name="Admin",
        role="admin",
        is_active=True,
        created_at=utcnow(),
    )
    api_db.add(user)
    api_db.commit()
    token = tenants.issue_magic_token(api_db, user.email)
    test_client = TestClient(api_module.app)
    assert test_client.get(f"/auth/verify?token={token}").status_code == 200
    return test_client


def _watcher_tick() -> str:
    """Тик серверного watcher'а: `pharmacy-monitor roi refresh --pending`."""
    result = CliRunner().invoke(main_mod.cli, ["roi", "refresh", "--pending"])
    assert result.exit_code == 0, result.output
    return result.output


def _recommendations(client):
    response = client.get("/api/v1/dash/roi/recommendations")
    assert response.status_code == 200, response.text
    return response.json()


_THRESHOLDS = {
    "raise_threshold_pct": 5.0,
    "undercut_threshold_pct": 3.0,
    "max_spread_pct": 80.0,
    "min_margin_pct": 10.0,
    "max_per_type": 10,
}


def test_new_thresholds_reach_recommendations_without_a_scrape(client, api_db):
    run = _full_run(api_db)
    # Клиент дешевле конкурентов на 6%: при пороге 5% это «подними цену».
    _cluster(api_db, run, "Aspirin", {"pharmonline": 10.0, "aptekonline": 10.6, "aloe": 10.6})
    roi_refresh.refresh_from_trusted_epoch(api_db)
    assert [item["type"] for item in _recommendations(client)["items"]] == ["price_raise"]
    runs_before = _run_count(api_db)

    saved = client.put(
        "/api/v1/dash/settings/pricing", json={**_THRESHOLDS, "raise_threshold_pct": 10.0}
    )
    assert saved.status_code == 200, saved.text

    # Старые рекомендации убраны сразу, и ответ честно говорит почему.
    hidden = client.get("/api/v1/dash/roi/recommendations")
    assert hidden.status_code == 503
    assert hidden.json()["detail"] == "Recommendations are being recalculated"
    assert client.get("/api/v1/dash/roi/actions").json()["detail"] == hidden.json()["detail"]
    status = client.get("/api/v1/dash/roi/status").json()
    assert (status["available"], status["refresh_pending"]) == (False, True)

    assert "пересчитано" in _watcher_tick()

    after = _recommendations(client)
    assert after["items"] == []
    assert after["provenance"]["run_id"] == run.id
    assert after["provenance"]["refresh_pending"] is False
    assert _run_count(api_db) == runs_before
    assert [(r.reason, r.status) for r in _requests(api_db)] == [("pricing_config", "done")]

    # И обратно: порог вернули — рекомендация вернулась, снова без сбора.
    client.put("/api/v1/dash/settings/pricing", json=_THRESHOLDS)
    _watcher_tick()
    assert [item["type"] for item in _recommendations(client)["items"]] == ["price_raise"]
    assert _run_count(api_db) == runs_before


def test_rejected_match_leaves_recommendations_without_a_scrape(client, api_db):
    run = _full_run(api_db)
    wrong = _cluster(
        api_db, run, "Paracetamol", {"pharmonline": 10.0, "aptekonline": 7.0, "aloe": 9.0}
    )
    _cluster(api_db, run, "Ibuprofen", {"pharmonline": 10.0, "aptekonline": 7.0, "aloe": 9.0})
    roi_refresh.refresh_from_trusted_epoch(api_db)
    assert _cached_types(api_db, "Paracetamol") == {"undercut"}
    runs_before = _run_count(api_db)

    assert client.post(f"/api/v1/dash/matches/{wrong.id}/reject").status_code == 204

    # До пересчёта дашборд не пустеет: прежний ответ и пометка «обновляется».
    stale = _recommendations(client)
    assert "Paracetamol" in {item["product_name"] for item in stale["items"]}
    assert stale["provenance"]["refresh_pending"] is True

    _watcher_tick()

    fresh = _recommendations(client)
    undercuts = {item["product_name"] for item in fresh["items"] if item["type"] == "undercut"}
    assert undercuts == {"Ibuprofen"}
    assert fresh["provenance"]["refresh_pending"] is False
    assert fresh["provenance"]["run_id"] == run.id
    assert _run_count(api_db) == runs_before
    assert [(r.reason, r.status) for r in _requests(api_db)] == [("match_reject", "done")]


def test_reject_does_not_reach_a_match_of_another_tenant(client, api_db):
    """Пара чужого тенанта по id не отклоняется: для пользователя её нет (404)."""
    foreign = Match(tenant_id=2, canonical_name="Paracetamol", confidence=1.0)
    api_db.add(foreign)
    api_db.flush()
    api_db.add_all(
        Product(
            tenant_id=2,
            site=site,
            external_id=f"foreign-{site}",
            url=f"http://{site}.az/p/foreign",
            name="Paracetamol",
            name_normalized="paracetamol",
            canonical_id=foreign.id,
        )
        for site in ("pharmonline", "aptekonline")
    )
    api_db.commit()
    foreign_id = foreign.id

    response = client.post(f"/api/v1/dash/matches/{foreign_id}/reject")

    assert response.status_code == 404
    # Ответ самого эндпоинта, а не роутера: тот же, что для несуществующего id.
    assert response.json() == {"detail": "Match not found"}
    api_db.expire_all()
    assert api_db.get(Match, foreign_id) is not None
    still_paired = api_db.scalars(select(Product.site).where(Product.canonical_id == foreign_id))
    assert sorted(still_paired) == ["aptekonline", "pharmonline"]
    assert api_db.scalar(select(func.count(MatchRejection.id))) == 0
    assert _requests(api_db) == []


def test_purchase_cost_import_and_its_rollback_reach_recommendations(client, api_db):
    run = _full_run(api_db)
    # Конкурент дешевле на 5%: «опусти до 9.49». Пока закупка неизвестна — warning.
    _cluster(api_db, run, "Losartan", {"pharmonline": 10.0, "aptekonline": 9.5, "aloe": 9.8})
    roi_refresh.refresh_from_trusted_epoch(api_db)
    runs_before = _run_count(api_db)

    def severity() -> str:
        (item,) = [i for i in _recommendations(client)["items"] if i["type"] == "undercut"]
        return item["severity"]

    def recalculating() -> bool:
        response = client.get("/api/v1/dash/roi/recommendations")
        return (
            response.status_code == 503
            and response.json()["detail"] == "Recommendations are being recalculated"
        )

    assert severity() == "warning"

    # Закупка 9.60 выше целевой цены: снижение стало бы продажей в убыток.
    imported = client.post(
        "/api/v1/dash/settings/costs/import",
        files={
            "file": (
                "costs.csv",
                b"sku,supplier_name,purchase_price,currency\n"
                b"pharmonline-Losartan,Vendor,9.60,AZN\n",
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["rows_imported"] == 1
    assert recalculating()
    _watcher_tick()
    assert severity() == "critical"

    rolled_back = client.post(
        f"/api/v1/dash/settings/costs/imports/{imported.json()['batch_id']}/rollback"
    )
    assert rolled_back.status_code == 200, rolled_back.text
    assert recalculating()
    _watcher_tick()
    assert severity() == "warning"

    assert _run_count(api_db) == runs_before
    assert [(r.reason, r.status) for r in _requests(api_db)] == [
        ("cost_import", "done"),
        ("cost_import_rollback", "done"),
    ]


def test_added_and_created_matches_queue_a_refresh(client, api_db):
    run = _full_run(api_db)
    match = _cluster(api_db, run, "Aspirin", {"pharmonline": 10.0, "aptekonline": 7.0})
    loose = {}
    for site in SITES:
        product = Product(
            tenant_id=1,
            site=site,
            external_id=f"{site}-loose",
            url=f"http://{site}.az/p/loose",
            name="Loose",
            name_normalized="loose",
        )
        api_db.add(product)
        api_db.flush()
        api_db.add(PriceSnapshot(run_id=run.id, product_id=product.id, price=9.0))
        loose[site] = product
    api_db.commit()

    added = client.post(
        f"/api/v1/dash/matches/{match.id}/add-product", json={"product_id": loose["aloe"].id}
    )
    assert added.status_code == 200, added.text
    created = client.post(
        "/api/v1/dash/matches/create-with-products",
        json={"product_ids": [loose["pharmonline"].id, loose["aptekonline"].id]},
    )
    assert created.status_code == 200, created.text

    assert [(r.reason, r.status) for r in _requests(api_db)] == [
        ("match_add_product", "pending"),
        ("match_create", "pending"),
    ]


def test_relink_queues_a_refresh_only_when_it_changes_the_pair(client, api_db):
    run = _full_run(api_db)
    match = _cluster(api_db, run, "Aspirin", {"pharmonline": 10.0, "aptekonline": 7.0})
    current = api_db.scalar(
        select(Product).where(Product.canonical_id == match.id, Product.site == "aptekonline")
    )
    other = Product(
        tenant_id=1,
        site="aptekonline",
        external_id="aspirin-other",
        url="http://aptekonline.az/p/aspirin-other",
        name="Aspirin other",
        name_normalized="aspirin other",
    )
    api_db.add(other)
    api_db.commit()

    # Ссылка на товар, который и так стоит в паре: ничего не меняется.
    refused = client.post(
        f"/api/v1/dash/matches/{match.id}/relink",
        json={"site": "aptekonline", "url": current.url},
    )
    assert refused.status_code == 400
    assert _requests(api_db) == []

    relinked = client.post(
        f"/api/v1/dash/matches/{match.id}/relink",
        json={"site": "aptekonline", "url": other.url},
    )
    assert relinked.status_code == 200, relinked.text
    assert [(r.reason, r.status) for r in _requests(api_db)] == [("match_relink", "pending")]


def test_confirm_does_not_queue_a_refresh(client, api_db):
    """`/confirm` меняет только флаги, которых расчёт рекомендаций не читает."""
    run = _full_run(api_db)
    match = _cluster(api_db, run, "Aspirin", {"pharmonline": 10.0, "aptekonline": 7.0})

    assert client.post(f"/api/v1/dash/matches/{match.id}/confirm").status_code == 204

    assert _requests(api_db) == []


def test_queued_refresh_is_not_promised_while_the_catalog_is_untrusted(client, api_db):
    """Заявка есть, но считать не от чего — ответ остаётся «ждите сбора»."""
    now = utcnow()
    run = _full_run(api_db, finished_at=now - timedelta(hours=2))
    _cluster(api_db, run, "Aspirin", {"pharmonline": 10.0, "aptekonline": 10.6, "aloe": 10.6})
    _full_run(api_db, ("pharmonline",), status="failed", finished_at=now - timedelta(hours=1))
    api_db.commit()

    assert client.put("/api/v1/dash/settings/pricing", json=_THRESHOLDS).status_code == 200

    response = client.get("/api/v1/dash/roi/recommendations")
    assert response.status_code == 503
    assert "Verified full-catalog" in response.json()["detail"]

    assert "не посчитано" in _watcher_tick()
    assert [r.status for r in _requests(api_db)] == ["skipped"]


_STAGE_SUMMARY = {"refreshed": 0, "relinked": 0, "clusters": 0, "revalidated": 0, "flagged": 0}


@pytest.mark.parametrize(
    ("args", "queued"),
    [
        # Штатный rematch — тот же этап сопоставления, что конец сбора.
        (["rematch"], ["rematch"]),
        (["rematch", "--reset", main_mod.REMATCH_RESET_CONFIRM_FLAG], ["rematch"]),
        (["rematch", "--revalidate"], ["rematch"]),
        (["rematch", "--revalidate", "--dry-run"], []),
        (["rematch", "--relink-dead"], ["rematch"]),
        (["rematch", "--relink-dead", "--dry-run"], []),
    ],
)
def test_rematch_queues_a_refresh_only_when_it_may_have_changed_pairs(
    api_db, monkeypatch, args, queued
):
    """Пары меняет и CLI: без заявки разбитая пара жила бы в рекомендациях до сбора."""
    from src import matcher

    monkeypatch.setattr(matcher, "revalidate_split", lambda session, dry_run=False: [])
    monkeypatch.setattr(matcher, "relink_dead_members", lambda session, dry_run=False: [])
    monkeypatch.setattr(
        main_mod, "_run_matching_stage", lambda session, fuzzy_threshold=None: _STAGE_SUMMARY
    )

    result = CliRunner().invoke(main_mod.cli, args)

    assert result.exit_code == 0, result.output
    assert [request.reason for request in _requests(api_db)] == queued


def test_interrupted_rematch_does_not_commit_half_done_pairs(api_db, monkeypatch):
    """Заявка ставится в `finally` — её коммит не должен записать недоделанное.

    Матчер правит `canonical_id` по ходу и коммитит в конце шага; до заявок
    прерванную работу откатывало закрытие сессии.
    """
    run = _full_run(api_db)
    match = _cluster(api_db, run, "Aspirin", {"pharmonline": 10.0, "aptekonline": 7.0})

    def interrupted(session, fuzzy_threshold=None):
        for product in session.scalars(select(Product).where(Product.canonical_id == match.id)):
            product.canonical_id = None
        session.flush()
        raise RuntimeError("identity revalidation failed: matcher died halfway")

    monkeypatch.setattr(main_mod, "_run_matching_stage", interrupted)

    result = CliRunner().invoke(main_mod.cli, ["rematch"])

    assert result.exit_code != 0
    api_db.expire_all()
    paired = api_db.scalars(select(Product).where(Product.canonical_id == match.id)).all()
    assert len(paired) == 2
    # Этап не прошёл — пары могли остаться непроверенными. Рекомендации по ним
    # публиковать нельзя: заявка не ставится, прежний кэш остаётся.
    assert _requests(api_db) == []


@pytest.mark.parametrize(
    ("flag", "step", "queued"),
    [
        # Разбиение упало: какие пары остались непроверенными — неизвестно.
        ("--revalidate", "revalidate_split", []),
        # Замены проверяются и коммитятся по одной: сделанное до сбоя уже в базе.
        ("--relink-dead", "relink_dead_members", ["rematch"]),
    ],
)
def test_failed_targeted_rematch_queues_only_for_work_already_committed(
    api_db, monkeypatch, flag, step, queued
):
    from src import matcher

    def boom(session, dry_run=False):
        raise RuntimeError("died halfway")

    monkeypatch.setattr(matcher, step, boom)

    result = CliRunner().invoke(main_mod.cli, ["rematch", flag])

    assert result.exit_code != 0
    assert [request.reason for request in _requests(api_db)] == queued


def test_rematch_request_does_not_commit_what_the_stage_left_unfinished(api_db, monkeypatch):
    """Шаг флагов упал, успев тронуть строки: этап это переживает и идёт дальше.

    Раньше недописанное откатывало закрытие сессии. Коммит заявки не должен
    записать его вместо этого.
    """
    run = _full_run(api_db)
    match = _cluster(api_db, run, "Aspirin", {"pharmonline": 10.0, "aptekonline": 7.0})

    def stage_whose_flag_step_died(session, fuzzy_threshold=None):
        session.get(Match, match.id).canonical_name = "half-written"
        session.flush()
        return {**_STAGE_SUMMARY, "flagged": None}

    monkeypatch.setattr(main_mod, "_run_matching_stage", stage_whose_flag_step_died)

    result = CliRunner().invoke(main_mod.cli, ["rematch"])

    assert result.exit_code == 0, result.output
    api_db.expire_all()
    assert api_db.get(Match, match.id).canonical_name == "Aspirin"
    assert [request.reason for request in _requests(api_db)] == ["rematch"]


def test_rematch_with_a_failed_preparation_step_still_queues_a_refresh(api_db, monkeypatch):
    """Сопоставление и revalidate прошли — пары проверены, хоть команда и вышла с ошибкой."""
    monkeypatch.setattr(
        main_mod,
        "_run_matching_stage",
        lambda session, fuzzy_threshold=None: {**_STAGE_SUMMARY, "relinked": None},
    )

    result = CliRunner().invoke(main_mod.cli, ["rematch"])

    assert result.exit_code != 0
    assert "preparation step failed" in result.output
    assert [request.reason for request in _requests(api_db)] == ["rematch"]


def test_rematch_waits_out_a_refresh_instead_of_skipping(api_db, monkeypatch):
    """Пропуск планового rematch никто не повторяет, а пересчёт идёт минуту."""
    attempts = iter([False, True])
    monkeypatch.setattr(
        main_mod, "_hold_scrape_lock_until_command_exit", lambda factory, wait: next(attempts)
    )
    monkeypatch.setattr(main_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        main_mod, "_run_matching_stage", lambda session, fuzzy_threshold=None: _STAGE_SUMMARY
    )

    result = CliRunner().invoke(main_mod.cli, ["rematch"])

    assert result.exit_code == 0, result.output
    assert "skipped" not in result.output


@pytest.mark.parametrize(
    ("extra_args", "scope", "queued"),
    [
        # Частичный прогон кэш не пишет, но его матчер пары менять может.
        (["--limit", "5"], "partial", ["partial_run"]),
        # Полный подтверждённый, но у других сайтов каталога нет: пересчитывать
        # не от чего — заявку закрыли бы `skipped`, поэтому её и не ставят.
        ([], "full", []),
    ],
)
def test_partial_run_queues_a_refresh_with_its_own_commit(
    db_session, monkeypatch, extra_args, scope, queued
):
    from tests.test_run_failure_semantics import _patch_verified_aloe_pipeline

    _patch_verified_aloe_pipeline(db_session, monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main_mod.cli,
            ["run", "--site", "aloe", "--mode", "category", "--no-alerts", *extra_args],
        )

    assert result.exit_code == 0, result.output
    db_session.expire_all()
    run = db_session.scalar(select(Run).order_by(Run.id.desc()))
    assert (run.catalog_scope, run.finished_at is not None) == (scope, True)
    assert [request.reason for request in _requests(db_session)] == queued


@pytest.mark.parametrize(
    ("extra_args", "failing_step", "queued"),
    [
        # Упал отчёт: пары уже закоммичены и проверены — заявка уходит с упавшим прогоном.
        (["--limit", "5"], "analyze", ["partial_run"]),
        # Упал сам этап сопоставления: пары не проверены — рекомендации по ним не считаем.
        (["--limit", "5"], "matching", []),
        # Упал полный подтверждённый прогон: он сам отменил доверие, считать не от чего.
        ([], "analyze", []),
    ],
)
def test_failed_run_owes_a_refresh_only_when_one_could_be_computed(
    db_session, monkeypatch, extra_args, failing_step, queued
):
    from tests.test_run_failure_semantics import _patch_verified_aloe_pipeline

    _patch_verified_aloe_pipeline(db_session, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError(f"{failing_step} failed")

    if failing_step == "analyze":
        monkeypatch.setattr(main_mod.analyzer, "analyze", boom)
    else:
        monkeypatch.setattr(main_mod, "_run_matching_stage", boom)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main_mod.cli,
            ["run", "--site", "aloe", "--mode", "category", "--no-alerts", *extra_args],
        )

    assert result.exit_code != 0
    db_session.expire_all()
    run = db_session.scalar(select(Run).order_by(Run.id.desc()))
    assert run.status == "failed"
    assert [request.reason for request in _requests(db_session)] == queued


def test_scrape_waits_out_a_short_catalog_reader_but_not_a_scrape(monkeypatch):
    """Пересчёт держит блокировку около минуты — intraday-тик не должен терять круг."""
    clock = {"now": 0.0}
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        main_mod.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds)
    )

    attempts = iter([False, False, True])
    monkeypatch.setattr(
        main_mod, "_hold_scrape_lock_until_command_exit", lambda factory, wait: next(attempts)
    )
    assert main_mod._hold_scrape_lock_after_readers(object()) is True
    assert clock["now"] == 2 * main_mod._SCRAPE_LOCK_POLL_SECONDS

    # Идущий сбор держит блокировку часами: ждём отсрочку и отказываем, как раньше.
    clock["now"] = 0.0
    monkeypatch.setattr(
        main_mod, "_hold_scrape_lock_until_command_exit", lambda factory, wait: False
    )
    assert main_mod._hold_scrape_lock_after_readers(object()) is False
    assert clock["now"] == main_mod._SCRAPE_LOCK_READER_GRACE_SECONDS

    # Отсрочка — минуты: её хватает на пересчёт, и она не съедает предел, который
    # systemd даёт плановому rematch (а многочасовой сбор так не переждать).
    import re
    from pathlib import Path

    unit = Path(__file__).resolve().parents[1] / "infra/systemd/pharmacy-monitor-rematch.service"
    unit_limit = int(re.search(r"^TimeoutStartSec=(\d+)", unit.read_text(), re.M).group(1))
    assert 60 <= main_mod._SCRAPE_LOCK_READER_GRACE_SECONDS <= 300 < unit_limit


def test_manual_cli_refresh_reports_refusal_with_nonzero_exit(api_db):
    result = CliRunner().invoke(main_mod.cli, ["roi", "refresh"])

    assert result.exit_code == 1
    assert "нет свежего подтверждённого каталога" in result.output
    assert "pharmonline:no_full_scan" in result.output


def test_watcher_cli_exits_nonzero_when_a_refresh_fails(api_db, monkeypatch):
    run = _full_run(api_db)
    _cluster(api_db, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    _queue(api_db)
    monkeypatch.setattr(
        roi,
        "_compute_actions_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = CliRunner().invoke(main_mod.cli, ["roi", "refresh", "--pending"])

    assert result.exit_code == 1
    assert "пересчёт упал" in result.output


def test_watcher_cli_serves_every_tenant_that_has_requests(api_db):
    roi_refresh.request_refresh(api_db, tenant_id=2, reason="other_tenant")
    api_db.commit()

    output = _watcher_tick()

    assert "tenant 2" in output
    # У тенанта 2 нет каталога — заявка разобрана и закрыта, а не забыта.
    assert [(r.tenant_id, r.status) for r in _requests(api_db)] == [(2, "skipped")]


def test_watcher_cli_is_silent_success_without_requests(api_db):
    assert "заявок нет" in _watcher_tick()


# ─── Сторож очереди в health-check ───────────────────────────────────────────


def _health_codes(s) -> set[str]:
    from src.health import check_health

    return {issue.code for issue in check_health(s).issues}


def _pending_since(s, **ago) -> RoiRefreshRequest:
    request = RoiRefreshRequest(
        tenant_id=1,
        reason="pricing_config",
        status="pending",
        requested_at=utcnow() - timedelta(**ago),
    )
    s.add(request)
    s.commit()
    return request


def test_health_flags_a_request_nobody_executes(db_session):
    """Watcher встал: заявка ждёт, хотя сборы не идут."""
    run = _full_run(db_session, finished_at=utcnow() - timedelta(hours=5))
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    request = _pending_since(db_session, minutes=25)
    assert "roi_refresh_stuck" not in _health_codes(db_session)

    request.requested_at = utcnow() - timedelta(minutes=35)
    db_session.commit()
    assert "roi_refresh_stuck" in _health_codes(db_session)


def test_health_does_not_count_the_time_a_scrape_was_running(db_session):
    """Пока идёт сбор, заявка ждёт по делу — сколько бы он ни шёл."""
    run = _full_run(db_session, finished_at=utcnow() - timedelta(hours=9))
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    _pending_since(db_session, hours=4)
    scrape = Run(
        tenant_id=1,
        started_at=utcnow() - timedelta(hours=5),
        finished_at=None,
        status="running",
    )
    db_session.add(scrape)
    db_session.commit()
    assert "roi_refresh_stuck" not in _health_codes(db_session)

    # Сбор только что закончился: тик watcher'а ещё просто не наступил.
    scrape.status = "ok"
    scrape.finished_at = utcnow() - timedelta(minutes=10)
    db_session.commit()
    assert "roi_refresh_stuck" not in _health_codes(db_session)

    scrape.finished_at = utcnow() - timedelta(minutes=40)
    db_session.commit()
    assert "roi_refresh_stuck" in _health_codes(db_session)


def test_orphan_run_does_not_silence_the_watchdog_forever(db_session):
    """Сирот убирает сам watcher. Встал он — «идущий» прогон не должен глушить сторожа."""
    run = _full_run(db_session, finished_at=utcnow() - timedelta(hours=20))
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    _pending_since(db_session, hours=12)
    db_session.add(Run(tenant_id=1, started_at=utcnow() - timedelta(hours=13), status="running"))
    db_session.commit()

    # Прогон начался раньше заявки и «идёт» до сих пор. Сбор дольше десяти
    # часов не живёт: последние три часа — уже простой, а не ожидание по делу.
    assert "roi_refresh_stuck" in _health_codes(db_session)


def test_scrape_of_another_tenant_counts_as_waiting_for_a_reason(db_session):
    """Блокировка сбора одна на всех тенантов — чужой сбор пересчёт тоже ждёт."""
    run = _full_run(db_session, finished_at=utcnow() - timedelta(hours=9))
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    _pending_since(db_session, hours=4)
    db_session.add(Run(tenant_id=2, started_at=utcnow() - timedelta(hours=5), status="running"))
    db_session.commit()

    assert "roi_refresh_stuck" not in _health_codes(db_session)


def test_overlapping_runs_are_not_counted_twice(db_session):
    """Сбор с GitHub и тик на сервере могут идти одновременно — время одно."""
    run = _full_run(db_session, finished_at=utcnow() - timedelta(hours=12))
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    _pending_since(db_session, hours=3)
    now = utcnow()
    for started_ago, finished_ago in ((3.0, 1.0), (2.5, 1.5)):
        db_session.add(
            Run(
                tenant_id=1,
                started_at=now - timedelta(hours=started_ago),
                finished_at=now - timedelta(hours=finished_ago),
                status="ok",
            )
        )
    db_session.commit()

    # Последний час не шло ничего. Сложи длительности прогонов — вышло бы, что
    # заявка все три часа ждала «по делу».
    assert "roi_refresh_stuck" in _health_codes(db_session)


def test_short_ticks_do_not_hide_a_dead_watcher(db_session):
    """Минутные частичные тики идут весь день; ожидание они не обнуляют."""
    run = _full_run(db_session, finished_at=utcnow() - timedelta(hours=12))
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    _pending_since(db_session, hours=10)
    for hours_ago in range(1, 10):
        started = utcnow() - timedelta(hours=hours_ago)
        db_session.add(
            Run(
                tenant_id=1,
                started_at=started,
                finished_at=started + timedelta(minutes=2),
                status="ok",
                catalog_scope="partial",
            )
        )
    # И один идёт прямо сейчас — как тик, стартующий в :00 вместе с проверкой.
    db_session.add(Run(tenant_id=1, started_at=utcnow() - timedelta(seconds=20), status="running"))
    db_session.commit()

    assert "roi_refresh_stuck" in _health_codes(db_session)


def test_health_flags_a_failed_refresh_until_the_cache_is_rewritten(db_session, monkeypatch):
    run = _full_run(db_session)
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0, "aloe": 6.5})
    assert roi_refresh.run_refresh(db_session).outcome == "refreshed"
    assert "roi_refresh_failed" not in _health_codes(db_session)

    _queue(db_session)
    real_compute = roi._compute_actions_locked

    def compute(session, *, client_site=None, **kwargs):
        if client_site == "aloe":
            raise RuntimeError("boom")
        return real_compute(session, client_site=client_site, **kwargs)

    monkeypatch.setattr(roi, "_compute_actions_locked", compute)
    assert roi_refresh.run_refresh(db_session, only_if_requested=True).outcome == "failed"
    # В очереди стоит повтор: упавший пересчёт ещё не итог, тревожить рано.
    assert "roi_refresh_failed" not in _health_codes(db_session)

    assert roi_refresh.run_refresh(db_session, only_if_requested=True).outcome == "failed"
    assert "roi_refresh_failed" in _health_codes(db_session)

    # Следующий удачный пересчёт (или полный сбор) переписывает кэш — тревога уходит.
    monkeypatch.setattr(roi, "_compute_actions_locked", real_compute)
    assert roi_refresh.run_refresh(db_session).outcome == "refreshed"
    assert "roi_refresh_failed" not in _health_codes(db_session)


def test_health_is_quiet_about_a_skipped_request(db_session):
    """«Каталог не подтверждён» — не поломка очереди; об этом кричат другие проверки."""
    now = utcnow()
    run = _full_run(db_session, finished_at=now - timedelta(hours=2))
    _cluster(db_session, run, "Aspirin", {"pharmonline": 5.0, "aptekonline": 7.0})
    _full_run(db_session, ("pharmonline",), status="failed", finished_at=now - timedelta(hours=1))
    db_session.commit()
    _queue(db_session)
    assert roi_refresh.run_refresh(db_session, only_if_requested=True).outcome == "untrusted"

    assert not {"roi_refresh_stuck", "roi_refresh_failed"} & _health_codes(db_session)


# ─── Договор с фронтендом и миграция ─────────────────────────────────────────


def test_frontend_recognises_both_unavailable_answers_by_the_exact_text():
    """Фронтенд различает «ждите сбора» и «пересчитываются» по строке detail.

    vitest в CI не запускается, поэтому расхождение строк ловится здесь: иначе
    вместо «пересчитываются» пользователь увидел бы красную ошибку 503.
    """
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "frontend/src/lib/api.ts").read_text()

    def constant(name: str) -> str:
        return re.search(rf'const {name} =\s*"([^"]+)";', source).group(1)

    assert constant("VERIFIED_SCAN_PENDING_DETAIL") == api_module._ROI_WAITING_FOR_SCAN_DETAIL
    assert constant("RECOMMENDATIONS_RECALCULATING_DETAIL") == api_module._ROI_RECALCULATING_DETAIL


def _alembic(db_url: str, *args: str) -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DATABASE_URL": db_url},
        check=True,
        capture_output=True,
        text=True,
    )


def _queue_table(db_url: str):
    from sqlalchemy import create_engine, inspect

    inspector = inspect(create_engine(db_url))
    if "roi_refresh_requests" not in inspector.get_table_names():
        return None
    return (
        {column["name"] for column in inspector.get_columns("roi_refresh_requests")},
        {index["name"] for index in inspector.get_indexes("roi_refresh_requests")},
    )


def test_migration_creates_the_queue_exactly_as_the_model_declares_it(tmp_path):
    """Как на проде: база на предыдущей ревизии, таблицы очереди ещё нет.

    На чистой базе её создаёт уже базовая миграция (`create_all` по моделям),
    поэтому прод-состояние получаем, убрав таблицу после подъёма до 0023.
    """
    from sqlalchemy import create_engine, text

    db_url = f"sqlite:///{tmp_path / 'queue.sqlite'}"
    _alembic(db_url, "upgrade", "0023_snapshot_confirmed_run")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE roi_refresh_requests"))
    engine.dispose()
    assert _queue_table(db_url) is None

    _alembic(db_url, "upgrade", "0024_roi_refresh_requests")

    table = RoiRefreshRequest.__table__
    assert _queue_table(db_url) == (
        {column.name for column in table.columns},
        {index.name for index in table.indexes},
    )

    _alembic(db_url, "downgrade", "0023_snapshot_confirmed_run")
    assert _queue_table(db_url) is None


def test_migration_adopts_a_table_already_created_from_the_models(tmp_path):
    """На проде таблицу может первым создать `init_db()` любого CLI-тика.

    `create_all` идёт в каждой команде, а watcher зовёт команды раз в минуту —
    раньше, чем выкладка дойдёт до шага миграций. Миграция, пришедшая следом,
    не должна упасть на «таблица уже есть».
    """
    db_url = f"sqlite:///{tmp_path / 'queue-precreated.sqlite'}"
    _alembic(db_url, "upgrade", "0023_snapshot_confirmed_run")
    created_by_model = _queue_table(db_url)
    assert created_by_model is not None

    _alembic(db_url, "upgrade", "0024_roi_refresh_requests")

    assert _queue_table(db_url) == created_by_model
