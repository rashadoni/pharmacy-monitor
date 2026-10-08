"""Плановый сбор pharmonline сам допускает простые случаи — и только их.

До 2026-10-08 допуск писала одна ручная сверка, и полный сбор отказывал на
первом же новом товаре сайта. Здесь закреплено, что именно сбор теперь
допускает сам (новый товар; товар, вернувшийся под тем же идентификатором и
адресом) и что по-прежнему останавливает его до ручной сверки: смена адреса у
идентификатора, перекодировка, чужой тенант, конфликт записей, всплеск.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from structlog.testing import capture_logs
from sqlalchemy.orm import sessionmaker

from src import main as main_mod
from src import notifications, storage
from src.normalize import normalize_name
from src.scrapers.base import RouteStatus, ScrapedProduct, ScrapeResult
from src.scrapers.pharmonline_public_api import (
    PUBLIC_API_AVAILABILITY_SOURCE,
    PUBLIC_CATALOG_ROUTE,
)
from tests.test_cadence_guard import SCRAPE_UNIT, _unit_run_args

SCHEDULED = main_mod._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_PROOF_VERSION
MANUAL = main_mod._PHARMONLINE_PUBLIC_API_ADMISSION_PROOF_VERSION
MANUAL_DIRECT = main_mod._PHARMONLINE_PUBLIC_API_MANUAL_DIRECT_ADMISSION_PROOF_VERSION
DDP = "pharmonline_ddp_total_count"


def _meteor_id(index: int) -> str:
    """17 знаков, как у родного идентификатора сайта."""
    return f"AdmissionTst{index:05d}"


def _url(slug: str) -> str:
    return f"https://pharmonline.az/product/{slug}"


def _api(index: int, slug: str, *, name: str | None = None, barcode: str | None = None):
    """Товар так, как его отдаёт проверенный публичный каталог."""
    return ScrapedProduct(
        site="pharmonline",
        external_id=_meteor_id(index),
        url=_url(slug),
        name=name or f"Product {slug}",
        price=10.0 + index,
        identity_verified=True,
        availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
        barcode=barcode,
    )


def _stored(
    index: int,
    slug: str,
    *,
    tenant_id: int = 1,
    availability_source: str | None = DDP,
    external_id: str | None = None,
    name: str | None = None,
    barcode: str | None = None,
) -> storage.Product:
    name = name or f"Product {slug}"
    return storage.Product(
        tenant_id=tenant_id,
        site="pharmonline",
        external_id=external_id or _meteor_id(index),
        url=_url(slug),
        name=name,
        name_normalized=normalize_name(name),
        availability_source=availability_source,
        barcode=barcode,
    )


def _catalog(*products: ScrapedProduct) -> list[ScrapeResult]:
    return [ScrapeResult(site="pharmonline", products=list(products))]


def _admit(db_session, results, *, run_id: int = 501, transport: str = "direct"):
    return main_mod._admit_pharmonline_public_api_scheduled_identities(
        db_session,
        results,
        tenant_id=1,
        run_id=run_id,
        source_transport=transport,
    )


def _admissions(db_session) -> list[storage.PharmonlinePublicAPIIdentityAdmission]:
    db_session.expire_all()
    return (
        db_session.query(storage.PharmonlinePublicAPIIdentityAdmission)
        .order_by(storage.PharmonlinePublicAPIIdentityAdmission.id)
        .all()
    )


def _product_count(db_session) -> int:
    db_session.expire_all()
    return db_session.query(storage.Product).count()


def _assert_nothing_written(db_session, *, products: int) -> None:
    assert _admissions(db_session) == []
    assert _product_count(db_session) == products
    assert db_session.query(storage.PharmonlinePublicAPIIdentityReconciliation).count() == 0
    assert db_session.query(storage.PharmonlinePublicAPIIdentityQuarantine).count() == 0


def test_proof_version_strings_are_a_data_contract():
    """Эти строки лежат в журнале допусков на проде. Переименовать константу —
    значит объявить все прежние допуски недействительными: сбор откажет, а
    ручная сверка упадёт на уникальном ключе журнала. Менять только вместе с
    данными."""
    assert SCHEDULED == "public_api_scheduled_admission_v1"
    assert MANUAL == "public_api_identity_admission_v1"
    # Третья версия — ручная сверка напрямую; её договор закреплён в
    # tests/test_pharmonline_direct_reconciliation.py.
    assert set(main_mod._PHARMONLINE_PUBLIC_API_ADMISSION_RULES_BY_PROOF) == {
        SCHEDULED,
        MANUAL,
        MANUAL_DIRECT,
    }
    scheduled_transports, scheduled_kinds = (
        main_mod._PHARMONLINE_PUBLIC_API_ADMISSION_RULES_BY_PROOF[SCHEDULED]
    )
    # Писателю без присмотра — только транспорты плановых путей и два класса.
    assert scheduled_transports == {"decodo", "direct", "firecrawl"}
    assert scheduled_kinds == {"new_public_product", "existing_native_id"}
    manual_transports, manual_kinds = main_mod._PHARMONLINE_PUBLIC_API_ADMISSION_RULES_BY_PROOF[
        MANUAL
    ]
    assert manual_transports == {"crawlbase", "decodo", "scraperapi", "firecrawl"}
    assert manual_kinds == {
        "new_public_product",
        "existing_native_id",
        "quarantined_public_product",
    }


def test_manual_reconciliation_script_names_its_proof_version():
    """У записи допуска нет версии по умолчанию. Ручная сверка обязана называть
    свою — иначе её workflow упадёт уже на проде, на вызове записи. Версию она
    берёт по транспорту: любым транспортом ручной сверки — прежнюю, напрямую —
    свою (tests/test_pharmonline_direct_reconciliation.py)."""
    import ast
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / (
        "scripts/reconcile_pharmonline_public_api_identities.py"
    )
    calls = [
        node
        for node in ast.walk(ast.parse(script.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "_apply_pharmonline_public_api_reconciliation"
    ]
    (call,) = calls
    (keyword,) = [kw for kw in call.keywords if kw.arg == "admission_proof_version"]
    assert getattr(keyword.value, "id", "") == "admission_proof_version"
    # Имя присвоено один раз — из транспорта, функцией, которая знает обе версии.
    tree = ast.parse(script.read_text(encoding="utf-8"))
    (assigned,) = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", "") == "admission_proof_version" for target in node.targets)
    ]
    assert isinstance(assigned, ast.Call)
    assert assigned.func.id == "_pharmonline_public_api_manual_admission_proof_version"
    assert [getattr(arg, "id", "") for arg in assigned.args] == ["transport"]

    import scripts.reconcile_pharmonline_public_api_identities as reconcile

    version_for = reconcile._pharmonline_public_api_manual_admission_proof_version
    assert version_for is main_mod._pharmonline_public_api_manual_admission_proof_version
    assert version_for("decodo") == MANUAL


def test_write_without_a_named_proof_version_is_a_programming_error(db_session):
    with pytest.raises(TypeError, match="admission_proof_version"):
        main_mod._apply_pharmonline_public_api_reconciliation(
            db_session,
            _catalog(_api(2, "brand-new")),
            tenant_id=1,
            source_manifest_sha256="c" * 64,
            catalog_fingerprint_sha256="d" * 64,
            source_transport="decodo",
            preflight_run_ref="501",
        )


# ─── Что допускается ─────────────────────────────────────────────────────────


def test_new_product_is_admitted_with_a_scheduled_proof(db_session):
    """Ни идентификатора, ни адреса в базе нет — сбор заводит товар и допуск сам."""
    db_session.add(_stored(1, "known"))
    db_session.commit()
    results = _catalog(_api(1, "known"), _api(2, "brand-new"))

    summary, admitted = _admit(db_session, results, run_id=501)

    assert summary["status"] == "admitted"
    assert (summary["new_public_product"], summary["existing_native_id"]) == (1, 0)
    assert [action.public_api_external_id for action in admitted] == [_meteor_id(2)]
    (admission,) = _admissions(db_session)
    product = db_session.get(storage.Product, admission.product_id)
    assert (product.external_id, product.url) == (_meteor_id(2), _url("brand-new"))
    assert admission.admission_kind == "new_public_product"
    # Отметка, по которой автодопуск отличим от ручной сверки.
    assert admission.proof_version == SCHEDULED != MANUAL
    assert admission.source_transport == "direct"
    assert admission.preflight_run_ref == "501"
    assert admission.catalog_fingerprint_sha256 == (
        main_mod._pharmonline_public_api_catalog_fingerprint(results)
    )
    assert main_mod._PHARMONLINE_PUBLIC_API_PROOF_SHA_RE.fullmatch(admission.source_manifest_sha256)
    # Допуск заводит личность, а не цену: цену пишет сам сбор.
    assert db_session.query(storage.PriceSnapshot).count() == 0
    assert main_mod._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1) == 2


def test_returned_product_with_the_same_id_and_url_is_admitted(db_session):
    """Строка без DDP-истории, сайт называет ту же пару идентификатор+адрес."""
    untrusted = _stored(3, "returned", availability_source=None)
    db_session.add_all([_stored(1, "known"), untrusted])
    db_session.commit()
    results = _catalog(_api(1, "known"), _api(3, "returned"))

    summary, admitted = _admit(db_session, results)

    assert summary["status"] == "admitted"
    assert (summary["new_public_product"], summary["existing_native_id"]) == (0, 1)
    (admission,) = _admissions(db_session)
    assert admission.product_id == untrusted.id
    assert admission.admission_kind == "existing_native_id"
    assert admission.proof_version == SCHEDULED
    assert _product_count(db_session) == 2  # новой строки нет: допущена прежняя
    assert len(admitted) == 1


def test_catalog_without_new_products_writes_nothing(db_session):
    db_session.add(_stored(1, "known"))
    db_session.commit()

    summary, admitted = _admit(db_session, _catalog(_api(1, "known")))

    assert summary["status"] == "nothing_to_admit"
    assert summary["reason"] is None
    assert admitted == []
    _assert_nothing_written(db_session, products=1)


def test_second_run_trusts_the_scheduled_admission(db_session):
    """Допуск планового сбора — постоянный: следующий сбор новым его не считает."""
    results = _catalog(_api(2, "brand-new"))
    assert _admit(db_session, results, run_id=501)[0]["status"] == "admitted"

    summary, admitted = _admit(db_session, results, run_id=502)

    assert summary["status"] == "nothing_to_admit"
    assert admitted == []
    assert [row.preflight_run_ref for row in _admissions(db_session)] == ["501"]


# ─── Что по-прежнему отказывает ──────────────────────────────────────────────


def test_url_change_of_a_known_id_refuses_everything(db_session):
    """Смена адреса у идентификатора — не простой допуск. Новый товар рядом
    тоже не допускается: план либо безопасен целиком, либо не применяется."""
    db_session.add(_stored(1, "old-address"))
    db_session.commit()
    results = _catalog(_api(1, "new-address"), _api(2, "brand-new"))

    summary, admitted = _admit(db_session, results)

    assert summary["status"] == "refused"
    assert summary["reason"] == "plan_unsafe:native_id_url_rebind_unproven=1"
    assert admitted == []
    _assert_nothing_written(db_session, products=1)
    with pytest.raises(main_mod.PharmonlinePublicAPIIdentityError, match="mismatched_urls=1"):
        main_mod._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1)


def test_url_change_proven_by_barcode_and_name_still_needs_reconciliation(db_session):
    """Даже доказанная двумя признаками смена адреса — переход личности, а не
    допуск: его выполняет только ручная сверка."""
    db_session.add(_stored(1, "old-address", name="Same medicine 500 mg", barcode="4600000000017"))
    db_session.commit()
    results = _catalog(
        _api(1, "new-address", name="Same medicine 500 mg", barcode="4600000000017"),
        _api(2, "brand-new"),
    )

    summary, _ = _admit(db_session, results)

    assert summary["status"] == "refused"
    assert summary["reason"] == (
        "identity_transition_required:legacy_rekeys=0,url_rebinds=1,splits=0"
    )
    _assert_nothing_written(db_session, products=1)
    stored = db_session.query(storage.Product).one()
    assert stored.url == _url("old-address")


def test_legacy_slug_row_at_the_same_url_refuses(db_session):
    """Перекодировка легаси-строки (slug вместо идентификатора) — за ручной сверкой."""
    db_session.add(
        _stored(1, "legacy-product", external_id="legacy-product", availability_source=None)
    )
    db_session.commit()

    summary, _ = _admit(db_session, _catalog(_api(1, "legacy-product"), _api(2, "brand-new")))

    assert summary["status"] == "refused"
    assert summary["reason"] == (
        "identity_transition_required:legacy_rekeys=1,url_rebinds=0,splits=0"
    )
    _assert_nothing_written(db_session, products=1)
    assert db_session.query(storage.Product).one().external_id == "legacy-product"


def test_id_owned_by_another_tenant_refuses(db_session):
    db_session.add(_stored(2, "foreign", tenant_id=2))
    db_session.commit()

    summary, _ = _admit(db_session, _catalog(_api(2, "foreign")))

    assert summary["status"] == "refused"
    assert summary["reason"] == "plan_unsafe:target_id_cross_tenant=1"
    _assert_nothing_written(db_session, products=1)


def test_url_owned_by_another_tenant_refuses(db_session):
    db_session.add(_stored(9, "shared-address", tenant_id=2))
    db_session.commit()

    summary, _ = _admit(db_session, _catalog(_api(2, "shared-address")))

    assert summary["status"] == "refused"
    assert summary["reason"] == "plan_unsafe:admission_url_cross_tenant=1"
    _assert_nothing_written(db_session, products=1)


def test_returned_product_sharing_its_url_with_another_native_row_refuses(db_session):
    db_session.add_all(
        [
            _stored(3, "contested", availability_source=None),
            _stored(4, "contested", availability_source=None),
        ]
    )
    db_session.commit()

    summary, _ = _admit(db_session, _catalog(_api(3, "contested")))

    assert summary["status"] == "refused"
    assert summary["reason"] == "plan_unsafe:admission_url_conflict=1"
    _assert_nothing_written(db_session, products=2)


def test_returned_product_with_an_unusable_ledger_row_refuses(db_session):
    """Запись в журнале есть, но проверка её не признаёт (незнакомая версия
    доказательства). Второй допуск поверх неё сбор не пишет."""
    untrusted = _stored(3, "returned", availability_source=None)
    db_session.add(untrusted)
    db_session.flush()
    db_session.add(
        storage.PharmonlinePublicAPIIdentityAdmission(
            tenant_id=1,
            product_id=untrusted.id,
            admission_kind="existing_native_id",
            public_api_external_id=_meteor_id(3),
            public_api_canonical_url=_url("returned"),
            proof_version="some_future_proof_v9",
            source_manifest_sha256="a" * 64,
            catalog_fingerprint_sha256="b" * 64,
            source_transport="direct",
            preflight_run_ref="1",
        )
    )
    db_session.commit()

    summary, _ = _admit(db_session, _catalog(_api(3, "returned")))

    assert summary["status"] == "refused"
    assert summary["reason"] == "existing_identity_has_prior_audit=1"
    assert [row.proof_version for row in _admissions(db_session)] == ["some_future_proof_v9"]


@pytest.mark.parametrize("transport", ["tor", "crawlbase", "scraperapi"])
def test_transport_outside_the_scheduled_paths_refuses(db_session, transport):
    """Незнакомый транспорт и транспорты старых ручных workflow: писать допуск
    без присмотра можно только с тех, которыми ходит плановый сбор."""
    summary, _ = _admit(db_session, _catalog(_api(2, "brand-new")), transport=transport)

    assert summary["status"] == "refused"
    assert summary["reason"] == "transport"
    _assert_nothing_written(db_session, products=0)


# ─── Потолок ─────────────────────────────────────────────────────────────────


def test_burst_above_the_limit_refuses_and_the_limit_itself_is_admitted(db_session, monkeypatch):
    monkeypatch.setenv(main_mod._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_LIMIT_ENV, "2")
    three = _catalog(_api(1, "new-1"), _api(2, "new-2"), _api(3, "new-3"))

    summary, _ = _admit(db_session, three)

    assert summary["status"] == "refused"
    assert summary["reason"] == "limit_exceeded:candidates=3,limit=2"
    _assert_nothing_written(db_session, products=0)

    summary, admitted = _admit(db_session, _catalog(_api(1, "new-1"), _api(2, "new-2")))

    assert summary["status"] == "admitted"
    assert len(admitted) == len(_admissions(db_session)) == 2


def test_default_limit_refuses_one_product_too_many(db_session, monkeypatch):
    """Потолок считает и новые, и вернувшиеся товары вместе."""
    monkeypatch.setattr(main_mod, "_PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_LIMIT", 3)
    db_session.add(_stored(4, "returned", availability_source=None))
    db_session.commit()
    results = _catalog(_api(1, "new-1"), _api(2, "new-2"), _api(3, "new-3"), _api(4, "returned"))

    summary, _ = _admit(db_session, results)

    assert summary["reason"] == "limit_exceeded:candidates=4,limit=3"
    _assert_nothing_written(db_session, products=1)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", main_mod._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_LIMIT),
        ("40", 40),
        ("0", 0),
        # Поднять потолок переменной нельзя — только снизить.
        ("100000", main_mod._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_LIMIT),
        # Опечатка в настройке ничего не допускает.
        ("many", 0),
        ("-5", 0),
        ("1.5", 0),
        # str.isdigit() принимает и такие «цифры», а int() на них падает.
        ("²", 0),
        ("①", 0),
        ("١٢", 0),
    ],
)
def test_limit_can_only_be_lowered_by_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv(main_mod._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_LIMIT_ENV, raw)

    assert main_mod._pharmonline_public_api_scheduled_admission_limit() == expected


def test_zero_limit_switches_auto_admission_off(db_session, monkeypatch):
    """Рубильник: с нулём сбор ведёт себя как до автодопуска."""
    monkeypatch.setenv(main_mod._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_LIMIT_ENV, "0")
    results = _catalog(_api(2, "brand-new"))

    summary, _ = _admit(db_session, results)

    assert summary["status"] == "refused"
    assert summary["reason"] == "disabled:candidates=1"
    _assert_nothing_written(db_session, products=0)
    with pytest.raises(main_mod.PharmonlinePublicAPIIdentityError, match="missing_trusted_ids=1"):
        main_mod._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1)


# ─── Транзакция ──────────────────────────────────────────────────────────────


def test_admission_is_rolled_back_when_the_catalog_still_fails_the_proof(db_session):
    """Каталог ниже нижней границы: дело не в новых товарах, допуск не остаётся."""
    db_session.add(_stored(1, "known"))
    db_session.add(
        storage.PharmonlinePublicAPICatalogBaseline(
            tenant_id=1,
            catalog_item_count=50,
            minimum_catalog_item_count=50,
            verified_identity_count=50,
            trusted_ddp_item_count=50,
            retired_ddp_item_count=0,
            reconciled_item_count=0,
            proof_version="url_continuity_v1",
            source_manifest_sha256="a" * 64,
            catalog_fingerprint_sha256="b" * 64,
            source_transport="decodo",
            preflight_run_ref="1",
        )
    )
    db_session.commit()
    results = _catalog(_api(1, "known"), _api(2, "brand-new"))

    summary, admitted = _admit(db_session, results)

    assert summary["status"] == "refused"
    assert summary["reason"] == "identity_proof_failed_after_admission"
    assert admitted == []
    _assert_nothing_written(db_session, products=1)
    with pytest.raises(
        main_mod.PharmonlinePublicAPIIdentityError,
        match="missing_trusted_ids=1.*catalog_floor_failed=1",
    ):
        main_mod._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1)


def test_state_changed_between_decision_and_write_refuses(db_session, monkeypatch):
    """Решение принято без блокировок; запись перечитывает план и сверяет хеш."""
    original_plan = main_mod._pharmonline_public_api_reconciliation_plan

    def plan_with_a_concurrent_writer(session, results, **kwargs):
        if kwargs.get("lock_products"):
            session.add(_stored(7, "appeared-meanwhile", availability_source=None))
            session.flush()
        return original_plan(session, results, **kwargs)

    monkeypatch.setattr(
        main_mod, "_pharmonline_public_api_reconciliation_plan", plan_with_a_concurrent_writer
    )

    summary, admitted = _admit(
        db_session, _catalog(_api(2, "brand-new"), _api(7, "appeared-meanwhile"))
    )

    assert summary["status"] == "refused"
    assert str(summary["reason"]).startswith("apply_refused:")
    assert "differs from its read-only proof" in str(summary["reason"])
    assert admitted == []
    _assert_nothing_written(db_session, products=0)


def test_database_error_during_write_refuses_without_leaking_row_values(db_session, monkeypatch):
    def broken_apply(*args, **kwargs):
        raise IntegrityError("INSERT INTO products ... secret-name", {}, Exception("duplicate"))

    monkeypatch.setattr(main_mod, "_apply_pharmonline_public_api_reconciliation", broken_apply)

    summary, admitted = _admit(db_session, _catalog(_api(2, "brand-new")))

    assert summary["status"] == "refused"
    assert summary["reason"] == "apply_failed:IntegrityError"
    assert admitted == []
    _assert_nothing_written(db_session, products=0)


def test_unexpected_error_inside_the_admission_leaves_nothing_behind(db_session, monkeypatch):
    """Не только «свои» исключения: любой сбой между первой записью и фиксацией
    откатывает допуск. Обработчик сбоя прогона фиксирует сессию, чтобы сохранить
    статус, — и без отката зафиксировал бы недописанный, непроверенный допуск."""

    def exploding_verify(*args, **kwargs):
        raise RuntimeError("unexpected bug after the admission was flushed")

    monkeypatch.setattr(main_mod, "_verify_pharmonline_public_api_identities", exploding_verify)

    with pytest.raises(RuntimeError, match="unexpected bug"):
        _admit(db_session, _catalog(_api(2, "brand-new")))
    # То, что сделал бы обработчик сбоя прогона.
    db_session.commit()

    _assert_nothing_written(db_session, products=0)


def test_ledger_collision_in_the_middle_of_a_batch_leaves_nothing_behind(db_session):
    """Настоящая ошибка базы посреди пачки: товары уже вставлены, а журнал
    отвергает допуск (на этот идентификатор есть старая непригодная строка)."""
    bystander = _stored(1, "known")
    db_session.add(bystander)
    db_session.flush()
    db_session.add(
        _ledger_row(bystander, public_api_external_id=_meteor_id(3), proof_version="stale_v0")
    )
    db_session.commit()
    results = _catalog(_api(1, "known"), _api(2, "new-2"), _api(3, "new-3"))

    summary, admitted = _admit(db_session, results)

    assert summary["status"] == "refused"
    assert summary["reason"] == "apply_failed:IntegrityError"
    assert admitted == []
    assert _product_count(db_session) == 1
    assert [row.proof_version for row in _admissions(db_session)] == ["stale_v0"]


def test_row_changed_after_the_decision_is_reread_under_the_lock(db_session, monkeypatch):
    """Строку поменяли между решением и записью, а объект от первого чтения ещё
    жив в сессии. План под блокировкой обязан прочитать базу, а не память."""
    returned = _stored(3, "returned", availability_source=None)
    db_session.add(returned)
    db_session.commit()
    original_plan = main_mod._pharmonline_public_api_reconciliation_plan
    held: list[storage.Product] = []

    def plan(session, results, **kwargs):
        if kwargs.get("lock_products"):
            # Другой писатель: мимо ORM, как это сделал бы чужой процесс
            # (update() по классу модели сам поправил бы объекты в сессии).
            products = storage.Product.__table__
            session.execute(
                update(products)
                .where(products.c.id == returned.id)
                .values(url=_url("moved-meanwhile"))
            )
        else:
            held.extend(session.query(storage.Product).all())
        return original_plan(session, results, **kwargs)

    monkeypatch.setattr(main_mod, "_pharmonline_public_api_reconciliation_plan", plan)

    summary, admitted = _admit(db_session, _catalog(_api(3, "returned")))

    assert held, "объект первого чтения должен оставаться в сессии"
    assert summary["status"] == "refused"
    assert "differs from its read-only proof" in str(summary["reason"])
    assert admitted == []
    assert _admissions(db_session) == []


# ─── Журнал ──────────────────────────────────────────────────────────────────


def test_admission_and_refusal_are_logged_without_product_data(db_session):
    """Автодопуск не бывает тихим. В событиях — только счётчики и обрезанные
    хеши: журнал еженедельного сбора открыт (шаг GitHub Actions)."""
    db_session.add(_stored(1, "old-address"))
    db_session.commit()

    with capture_logs() as refused_logs:
        _admit(db_session, _catalog(_api(1, "new-address"), _api(2, "secret-slug")))
    with capture_logs() as admitted_logs:
        _admit(db_session, _catalog(_api(1, "old-address"), _api(2, "secret-slug")), run_id=77)

    (refusal,) = [
        entry
        for entry in refused_logs
        if entry["event"] == "pharmonline_public_api_scheduled_admission_refused"
    ]
    assert refusal["log_level"] == "warning"
    assert refusal["reason"] == "plan_unsafe:native_id_url_rebind_unproven=1"
    (admission,) = [
        entry
        for entry in admitted_logs
        if entry["event"] == "pharmonline_public_api_scheduled_admission"
    ]
    assert admission["log_level"] == "warning"
    assert (admission["admitted"], admission["new_public_product"], admission["run_id"]) == (
        1,
        1,
        77,
    )
    assert admission["proof_version"] == SCHEDULED
    everything = repr(refused_logs) + repr(admitted_logs)
    assert "secret-slug" not in everything
    assert _meteor_id(2) not in everything


# ─── Запись допуска: версия доказательства и её границы ──────────────────────


def _apply(db_session, results, **overrides):
    kwargs = dict(
        tenant_id=1,
        source_manifest_sha256="c" * 64,
        catalog_fingerprint_sha256=main_mod._pharmonline_public_api_catalog_fingerprint(results),
        source_transport="direct",
        preflight_run_ref="501",
        admission_proof_version=SCHEDULED,
    )
    kwargs.update(overrides)
    return main_mod._apply_pharmonline_public_api_reconciliation(db_session, results, **kwargs)


def test_scheduled_proof_cannot_write_a_rekey_or_a_url_rebind(db_session):
    """Вторая линия у самой записи: версия планового сбора — только на допуски."""
    db_session.add(
        _stored(1, "legacy-product", external_id="legacy-product", availability_source=None)
    )
    db_session.commit()

    with pytest.raises(
        main_mod.PharmonlinePublicAPIReconciliationError, match="limited to plain admissions"
    ):
        _apply(db_session, _catalog(_api(1, "legacy-product")))
    db_session.rollback()

    _assert_nothing_written(db_session, products=1)
    assert db_session.query(storage.Product).one().external_id == "legacy-product"


def test_unknown_admission_proof_version_is_rejected_before_any_write(db_session):
    with pytest.raises(
        main_mod.PharmonlinePublicAPIReconciliationError, match="proof version is invalid"
    ):
        _apply(db_session, _catalog(_api(2, "brand-new")), admission_proof_version="made_up_v1")

    _assert_nothing_written(db_session, products=0)


def test_manual_proof_still_refuses_the_direct_transport(db_session):
    """Версия ручной сверки по-прежнему не принимает direct — ни при записи,
    ни при чтении журнала. Отметка планового сбора этого правила не сняла."""
    results = _catalog(_api(2, "brand-new"))

    with pytest.raises(
        main_mod.PharmonlinePublicAPIReconciliationError,
        match="requires valid immutable workflow evidence",
    ):
        _apply(db_session, results, admission_proof_version=MANUAL)

    _assert_nothing_written(db_session, products=0)


def _force_plan(monkeypatch, *admissions):
    """План, который заявляет новые товары, не глядя в базу.

    Настоящий план такого не выдаст — проверяется последняя линия у записи:
    она не должна полагаться на то, что план прав.
    """
    original_plan = main_mod._pharmonline_public_api_reconciliation_plan

    def plan(session, results, **kwargs):
        _actions, _admissions, quarantines, metrics = original_plan(session, results, **kwargs)
        blind = dict(metrics)
        blind.update(dict.fromkeys(main_mod._PHARMONLINE_PUBLIC_API_UNSAFE_PLAN_METRICS, 0))
        blind["classified_identities"] = blind["api_identities"]
        return [], list(admissions), quarantines, blind

    monkeypatch.setattr(main_mod, "_pharmonline_public_api_reconciliation_plan", plan)


def _genesis(product: ScrapedProduct):
    return main_mod._PharmonlinePublicAPIIdentityAdmissionAction(
        product_id=None,
        admission_kind="new_public_product",
        public_api_external_id=product.external_id,
        public_api_canonical_url=product.url,
        public_product=product,
    )


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param({"index": 2, "slug": "elsewhere"}, id="identifier-taken"),
        pytest.param({"index": 9, "slug": "brand-new"}, id="address-taken"),
        pytest.param(
            {"index": 9, "slug": "brand-new", "tenant_id": 2}, id="address-taken-by-tenant-2"
        ),
    ],
)
def test_write_refuses_a_new_product_whose_identity_is_already_stored(
    db_session, monkeypatch, stored
):
    db_session.add(_stored(stored.pop("index"), stored.pop("slug"), **stored))
    db_session.commit()
    public = _api(2, "brand-new")
    _force_plan(monkeypatch, _genesis(public))

    with pytest.raises(
        main_mod.PharmonlinePublicAPIReconciliationError,
        match="genesis identity exists before apply",
    ):
        _apply(db_session, _catalog(public))
    db_session.rollback()

    _assert_nothing_written(db_session, products=1)


def test_write_refuses_two_new_products_claiming_one_address(db_session, monkeypatch):
    """Занятые адреса пополняются вставками этой же пачки."""
    first = _api(2, "brand-new")
    second = _api(3, "brand-new")
    _force_plan(monkeypatch, _genesis(first), _genesis(second))

    with pytest.raises(
        main_mod.PharmonlinePublicAPIReconciliationError,
        match="genesis identity exists before apply",
    ):
        _apply(db_session, _catalog(first))
    db_session.rollback()

    _assert_nothing_written(db_session, products=0)


def _ledger_row(product: storage.Product, **overrides):
    values = dict(
        tenant_id=1,
        product_id=product.id,
        admission_kind="new_public_product",
        public_api_external_id=product.external_id,
        public_api_canonical_url=product.url,
        proof_version=SCHEDULED,
        source_manifest_sha256="a" * 64,
        catalog_fingerprint_sha256="b" * 64,
        source_transport="direct",
        preflight_run_ref="501",
    )
    values.update(overrides)
    return storage.PharmonlinePublicAPIIdentityAdmission(**values)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, None),
        ({"source_transport": "decodo"}, None),
        ({"source_transport": "firecrawl"}, None),
        # Транспорты старых ручных workflow плановой версии не положены.
        ({"source_transport": "crawlbase"}, "transport"),
        ({"source_transport": "scraperapi"}, "transport"),
        ({"admission_kind": "existing_native_id"}, None),
        ({"proof_version": MANUAL, "source_transport": "decodo"}, None),
        # Ручная версия с direct — такой строки ручная сверка написать не могла.
        ({"proof_version": MANUAL}, "transport"),
        ({"source_transport": "tor"}, "transport"),
        # Плановый сбор личности не разводит.
        ({"admission_kind": "quarantined_public_product"}, "admission_kind"),
        ({"proof_version": "public_api_scheduled_admission_v2"}, "proof_version"),
        ({"preflight_run_ref": "run-501"}, "preflight_run"),
        ({"source_manifest_sha256": "not-a-hash"}, "source_manifest"),
    ],
)
def test_ledger_row_validity_by_proof_version(db_session, overrides, reason):
    product = _stored(2, "brand-new", availability_source=None)
    db_session.add(product)
    db_session.flush()

    row = _ledger_row(product, **overrides)

    assert main_mod._pharmonline_public_api_admission_invalid_reason(row, product) == reason


def test_tampered_scheduled_admission_stops_being_trusted(db_session):
    """Доверие держится на строке журнала: с испорченной версией товар снова
    неизвестен, и сбор отказывает, а не допускает его второй раз."""
    results = _catalog(_api(2, "brand-new"))
    assert _admit(db_session, results)[0]["status"] == "admitted"
    (admission,) = _admissions(db_session)
    admission.proof_version = "tampered"
    db_session.commit()

    summary, _ = _admit(db_session, results)

    assert summary["status"] == "refused"
    assert summary["reason"] == "existing_identity_has_prior_audit=1"
    with pytest.raises(main_mod.PharmonlinePublicAPIIdentityError, match="missing_trusted_ids=1"):
        main_mod._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1)


# ─── Плановый сбор целиком: команда из юнита ─────────────────────────────────


class _AfterPersist(Exception):
    """Сбор записан — сопоставление, алерты и отчёт этим тестам не нужны."""


def _catalog_result(*products: ScrapedProduct) -> ScrapeResult:
    count = len(products)
    return ScrapeResult(
        site="pharmonline",
        products=list(products),
        items_expected=1,
        items_completed=1,
        item_results={PUBLIC_CATALOG_ROUTE: {"status": "ok", "products": count}},
        category_counts={PUBLIC_CATALOG_ROUTE: count},
        route_statuses={
            PUBLIC_CATALOG_ROUTE: RouteStatus(
                complete=True,
                expected_pages=1,
                visited_pages=1,
                raw_items=count,
                parsed_items=count,
                expected_items=count,
            )
        },
    )


@pytest.fixture
def scheduled_run(db_session, monkeypatch, tmp_path):
    """`run` как на проде: маркер автономного режима, транспорт direct.

    Возвращает функцию: каталог сайта → результат CLI. Письма складываются в
    `sent`.
    """
    sent: list[dict] = []
    catalog: list[ScrapedProduct] = []

    async def fake_scrape_all(sites_with_slugs, *args, **kwargs):
        assert sites_with_slugs == {"pharmonline": [PUBLIC_CATALOG_ROUTE]}
        return [_catalog_result(*catalog)]

    def stop_after_persist(session, *, wait):
        raise _AfterPersist

    def record_email(**kwargs):
        sent.append(kwargs)
        return True

    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    monkeypatch.setattr(storage, "make_session", lambda: factory)
    monkeypatch.setattr(main_mod, "maybe_seed_categories", lambda session: None)
    monkeypatch.setattr(main_mod, "baselines_for_sites", lambda *args: {})
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape_all)
    monkeypatch.setattr(main_mod, "_acquire_matcher_lock", stop_after_persist)
    monkeypatch.setattr(notifications.notifier, "send_email", record_email)
    # Автономный режим пишет в os.environ напрямую: ключи регистрируем через
    # setenv, иначе запись утечёт в следующие тесты.
    for name in (
        "PHARMONLINE_PUBLIC_API",
        "PHARMONLINE_PUBLIC_API_TRANSPORT",
        "PHARMONLINE_PUBLIC_API_REQUIRE_CATALOG_BASELINE",
        "PHARMONLINE_LEGACY_ID_BRIDGE",
        "PHARMONLINE_USE_DDP",
        "PHARMONLINE_DECODO_BACKCONNECT_STICKY",
        "AI_FALLBACK_ENABLED",
        "SCRAPE_REPORT_EMAIL",
        main_mod._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_LIMIT_ENV,
    ):
        monkeypatch.setenv(name, "")
    marker = tmp_path / "pharmonline-public-api-autonomous-v1"
    marker.write_text(main_mod._PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER_CONTENT, encoding="utf-8")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER", str(marker))
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_TRANSPORT", "direct")
    # Автономный режим требует нижнюю границу каталога.
    db_session.add(
        storage.PharmonlinePublicAPICatalogBaseline(
            tenant_id=1,
            catalog_item_count=1,
            minimum_catalog_item_count=1,
            verified_identity_count=1,
            trusted_ddp_item_count=1,
            retired_ddp_item_count=0,
            reconciled_item_count=0,
            proof_version="url_continuity_v1",
            source_manifest_sha256="a" * 64,
            catalog_fingerprint_sha256="b" * 64,
            source_transport="decodo",
            preflight_run_ref="1",
        )
    )
    db_session.add_all(
        [
            storage.TenantUser(tenant_id=1, email="owner@example.test", role="admin"),
            storage.TenantUser(tenant_id=1, email="client@example.test", role="viewer"),
        ]
    )
    db_session.commit()

    def invoke(*products: ScrapedProduct):
        catalog[:] = products
        return CliRunner().invoke(main_mod.cli, invoke.args)

    invoke.sent = sent
    # По умолчанию — команда из systemd-юнита, как на проде.
    invoke.args = _unit_run_args(SCRAPE_UNIT, "pharmonline")
    return invoke


def _latest_run(db_session) -> storage.Run:
    db_session.expire_all()
    return db_session.query(storage.Run).order_by(storage.Run.id.desc()).first()


def test_scheduled_run_admits_a_new_product_and_persists_the_catalog(db_session, scheduled_run):
    """Команда из systemd-юнита: новый товар больше не останавливает сбор."""
    db_session.add(_stored(1, "known"))
    db_session.commit()

    result = scheduled_run(_api(1, "known"), _api(2, "brand-new", name="Новый <товар> 500 мг"))

    # Сбор дошёл до сопоставления — значит, проверку личностей и запись прошёл.
    assert isinstance(result.exception, SystemExit), result.output
    assert "_AfterPersist" in result.output
    run = _latest_run(db_session)
    admission_note = run.run_quality["pharmonline_identity_admission"]
    assert admission_note["status"] == "admitted"
    assert admission_note["new_public_product"] == 1
    assert admission_note["proof_version"] == SCHEDULED
    assert "сам допустил 1 тов." in admission_note["note"]
    assert run.catalog_verified is True
    assert run.products_scraped == 2
    (admission,) = _admissions(db_session)
    assert admission.preflight_run_ref == str(run.id)
    assert admission.source_transport == "direct"
    # Запись сбора состоялась: у обоих товаров есть цена этого прогона.
    assert db_session.query(storage.PriceSnapshot).filter_by(run_id=run.id).count() == 2
    # Письмо — только администратору, с названием товара без сырого HTML.
    (mail,) = scheduled_run.sent
    assert mail["to"] == ["owner@example.test"]
    assert "сам допустил 1 тов." in mail["subject"]
    assert "Новый &lt;товар&gt; 500 мг" in mail["html_body"]
    assert _url("brand-new") in mail["html_body"]


def test_scheduled_run_still_stops_on_a_url_change(db_session, scheduled_run):
    db_session.add(_stored(1, "old-address"))
    db_session.commit()

    result = scheduled_run(_api(1, "new-address"), _api(2, "brand-new"))

    assert result.exit_code != 0
    run = _latest_run(db_session)
    assert run.status == "failed"
    assert run.catalog_verified is False
    verdict = "scheduled_admission=refused(plan_unsafe:native_id_url_rebind_unproven=1)"
    # Там, где смотрит админ: health и список прогонов показывают error_message.
    assert "mismatched_urls=1" in run.error_message
    assert verdict in run.error_message
    assert verdict in run.run_quality["catalog_verification_reason_full"]
    assert run.catalog_verification_reason.startswith("public_api_identity_proof_failed:")
    assert run.run_quality["pharmonline_identity_admission"]["status"] == "refused"
    _assert_nothing_written(db_session, products=1)
    assert db_session.query(storage.PriceSnapshot).count() == 0
    assert scheduled_run.sent == []


def test_scheduled_run_with_auto_admission_switched_off_fails_as_before(
    db_session, scheduled_run, monkeypatch
):
    monkeypatch.setenv(main_mod._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_LIMIT_ENV, "0")
    db_session.add(_stored(1, "known"))
    db_session.commit()

    result = scheduled_run(_api(1, "known"), _api(2, "brand-new"))

    assert result.exit_code != 0
    run = _latest_run(db_session)
    assert run.status == "failed"
    assert "missing_trusted_ids=1" in run.error_message
    assert "scheduled_admission=refused(disabled:candidates=1)" in run.error_message
    _assert_nothing_written(db_session, products=1)
    assert scheduled_run.sent == []


def test_scheduled_run_without_new_products_sends_no_mail(db_session, scheduled_run):
    db_session.add(_stored(1, "known"))
    db_session.commit()

    scheduled_run(_api(1, "known"))

    run = _latest_run(db_session)
    assert run.run_quality["pharmonline_identity_admission"]["status"] == "nothing_to_admit"
    assert run.catalog_verified is True
    assert _admissions(db_session) == []
    assert scheduled_run.sent == []


def test_run_failing_after_the_admission_keeps_it_and_shows_it(
    db_session, scheduled_run, monkeypatch
):
    """Допуск фиксируется до записи сбора. Сбор упал — остаются товар без цены и
    его допуск (как между ручной сверкой и следующим сбором), и прогон это
    показывает; следующий сбор товар уже не считает новым."""

    def broken_persist(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(main_mod, "persist_results", broken_persist)

    result = scheduled_run(_api(2, "brand-new"))

    assert result.exit_code != 0
    run = _latest_run(db_session)
    assert run.status == "failed"
    assert "database went away" in run.error_message
    assert run.run_quality["pharmonline_identity_admission"]["status"] == "admitted"
    assert run.run_quality["pharmonline_identity_admission"]["mail"] == {"email": 1, "failed": 0}
    # Упавший прогон не должен выглядеть пригодным для денежных выводов.
    assert run.run_quality["financially_eligible"] is False
    assert run.run_quality["full_catalog_verified"] is False
    # Форма та же, что у любого прогона: её читает дашборд.
    assert run.run_quality["mode"] == "public_api"
    assert "pharmonline" in run.run_quality["sites"]
    (admission,) = _admissions(db_session)
    assert admission.preflight_run_ref == str(run.id)
    assert db_session.get(storage.Product, admission.product_id) is not None
    assert db_session.query(storage.PriceSnapshot).count() == 0
    assert len(scheduled_run.sent) == 1

    monkeypatch.undo()
    summary, admitted = _admit(db_session, _catalog(_api(2, "brand-new")), run_id=run.id + 1)

    assert summary["status"] == "nothing_to_admit"
    assert admitted == []


def test_dry_run_admission_is_real_and_still_mails_the_admin(
    db_session, scheduled_run, monkeypatch
):
    """`--dry-run` придерживает то, что читает клиент (алерты, отчёт). Допуск
    при этом настоящий, и служебное письмо о нём уходит: с `--dry-run` зовут
    `run` workflow ручной сверки, и допуск, сделанный там, не должен быть тихим."""
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API", "required")
    monkeypatch.setenv("PHARMONLINE_PUBLIC_API_AUTONOMOUS_MARKER", "/nonexistent/marker")
    scheduled_run.args = ["run", "--site", "pharmonline", "--mode", "public_api", "--dry-run"]

    result = scheduled_run(_api(2, "brand-new"))

    assert "_AfterPersist" in result.output
    run = _latest_run(db_session)
    assert run.run_quality["pharmonline_identity_admission"]["status"] == "admitted"
    assert len(_admissions(db_session)) == 1
    (mail,) = scheduled_run.sent
    assert mail["to"] == ["owner@example.test"]
    assert run.run_quality["pharmonline_identity_admission"]["mail"] == {"email": 1, "failed": 0}


def test_run_without_a_qualifying_admin_still_admits_and_says_so(db_session, scheduled_run):
    """Некому писать — не повод не собирать: допуск остаётся в прогоне и журнале."""
    for user in db_session.query(storage.TenantUser).filter_by(role="admin"):
        user.email_severity_min = "critical"
    db_session.commit()

    result = scheduled_run(_api(2, "brand-new"))

    assert "_AfterPersist" in result.output
    assert scheduled_run.sent == []
    run = _latest_run(db_session)
    assert run.run_quality["pharmonline_identity_admission"]["status"] == "admitted"
    # CLI настраивает журнал сам, поэтому событие ищем в его выводе.
    assert "pharmonline_scheduled_admission_mail_no_recipient" in result.output
    # И в самом прогоне видно, что письма не было.
    assert run.run_quality["pharmonline_identity_admission"]["mail"] == {"email": 0, "failed": 0}


def test_failed_mail_does_not_stop_the_run(db_session, scheduled_run, monkeypatch):
    def broken_mail(**kwargs):
        raise RuntimeError("smtp is down")

    monkeypatch.setattr(notifications.notifier, "send_email", broken_mail)

    result = scheduled_run(_api(2, "brand-new"))

    assert "_AfterPersist" in result.output
    run = _latest_run(db_session)
    assert run.run_quality["pharmonline_identity_admission"]["status"] == "admitted"
    assert run.run_quality["pharmonline_identity_admission"]["mail"] == {"email": 0, "failed": 1}
    assert db_session.query(storage.PriceSnapshot).filter_by(run_id=run.id).count() == 1


# ─── Письмо ──────────────────────────────────────────────────────────────────


def test_admission_mail_lists_a_bounded_number_of_products(db_session, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(
        notifications.notifier, "send_email", lambda **kwargs: sent.append(kwargs) or True
    )
    db_session.add(storage.TenantUser(tenant_id=1, email="owner@example.test", role="owner"))
    db_session.add(
        storage.TenantUser(
            tenant_id=1, email="quiet@example.test", role="admin", email_severity_min="critical"
        )
    )
    db_session.add(
        storage.TenantUser(tenant_id=1, email="gone@example.test", role="admin", is_active=False)
    )
    db_session.add(storage.TenantUser(tenant_id=2, email="other@example.test", role="admin"))
    db_session.commit()
    total = notifications.PHARMONLINE_ADMISSION_MAIL_ROWS + 5
    items = [("new_public_product", f"Товар {i}", _url(f"p-{i}")) for i in range(total - 1)]
    items.append(("existing_native_id", "Вернувшийся", _url("returned")))

    counts = notifications.mail_pharmonline_admission_to_admins(
        db_session, tenant_id=1, run_id=77, items=items
    )

    assert counts == {"email": 1, "failed": 0}
    (mail,) = sent
    assert mail["to"] == ["owner@example.test"]
    body = mail["html_body"]
    assert f"новых — {total - 1}" in body
    assert "вернувшихся под прежним идентификатором и адресом — 1" in body
    assert "прогон #77" in body
    assert body.count("pharmonline.az/product/") == notifications.PHARMONLINE_ADMISSION_MAIL_ROWS
    assert "…и ещё 5." in body


def test_mail_failure_log_carries_no_address(db_session, monkeypatch):
    def refused_by_smtp(**kwargs):
        raise RuntimeError("{'owner@example.test': (550, b'mailbox unavailable')}")

    monkeypatch.setattr(notifications.notifier, "send_email", refused_by_smtp)
    db_session.add(storage.TenantUser(tenant_id=1, email="owner@example.test", role="admin"))
    db_session.commit()

    with capture_logs() as logs:
        counts = notifications.mail_pharmonline_admission_to_admins(
            db_session,
            tenant_id=1,
            run_id=77,
            items=[("new_public_product", "Товар", _url("p"))],
        )

    assert counts == {"email": 0, "failed": 1}
    assert "owner@example.test" not in repr(logs)
    assert [entry["event"] for entry in logs] == ["pharmonline_admission_mail_failed"]


def test_admission_mail_links_only_pharmonline_product_addresses():
    body = notifications._render_pharmonline_admission_email(
        77,
        [
            ("new_public_product", "Настоящий", _url("real")),
            ("new_public_product", "Чужой", "javascript:alert(1)"),
            ("new_public_product", "Подделка", "https://pharmonline.az.evil.test/product/x"),
        ],
    )

    assert f'href="{_url("real")}"' in body
    assert body.count("href=") == 1
    assert "Чужой" in body and "Подделка" in body


def test_admission_mail_is_not_sent_for_an_empty_list(db_session, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(
        notifications.notifier, "send_email", lambda **kwargs: sent.append(kwargs) or True
    )
    db_session.add(storage.TenantUser(tenant_id=1, email="owner@example.test", role="admin"))
    db_session.commit()

    counts = notifications.mail_pharmonline_admission_to_admins(
        db_session, tenant_id=1, run_id=77, items=[]
    )

    assert counts == {"email": 0, "failed": 0}
    assert sent == []


# ─── PostgreSQL: транзакция допуска на настоящей базе ────────────────────────


@pytest.fixture
def postgres_session():
    """Сессия на PostgreSQL из DATABASE_URL (в CI он есть); без него — пропуск.

    База общая для всех тестов, поэтому у теста свой тенант и свои
    идентификаторы, а строки за собой он удаляет.
    """
    import os
    import secrets

    from sqlalchemy import text

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL is required")
    tenant_id = 900_000 + secrets.randbelow(90_000)
    tag = secrets.token_hex(3)
    session = storage.make_session(database_url)()
    try:
        yield session, tenant_id, tag
    finally:
        session.rollback()
        for statement in (
            "DELETE FROM pharmonline_public_api_identity_admissions WHERE tenant_id = :t",
            "DELETE FROM products WHERE tenant_id = :t",
            "DELETE FROM runs WHERE tenant_id = :t",
        ):
            session.execute(text(statement), {"t": tenant_id})
        session.commit()
        session.close()


def _pg_api(tag: str, letter: str, slug: str) -> ScrapedProduct:
    return ScrapedProduct(
        site="pharmonline",
        external_id=f"PgAdm{tag}{letter * 6}",
        url=_url(f"pg-{tag}-{slug}"),
        name=f"Postgres {slug}",
        price=10.0,
        identity_verified=True,
        availability_source=PUBLIC_API_AVAILABILITY_SOURCE,
    )


def test_admission_runs_serializable_under_row_locks_on_postgres(postgres_session, monkeypatch):
    """То, чего SQLite не исполняет: SERIALIZABLE первой командой транзакции,
    блокировка строк, возврат к обычному уровню после фиксации."""
    from sqlalchemy import text

    session, tenant_id, tag = postgres_session
    run = storage.Run(status="running", tenant_id=tenant_id)
    session.add(run)
    known = _pg_api(tag, "k", "known")
    session.add(
        storage.Product(
            tenant_id=tenant_id,
            site="pharmonline",
            external_id=known.external_id,
            url=known.url,
            name=known.name,
            name_normalized=normalize_name(known.name),
            availability_source=DDP,
        )
    )
    session.commit()
    seen: dict[str, object] = {}
    original_apply = main_mod._apply_pharmonline_public_api_reconciliation

    def observing_apply(apply_session, results, **kwargs):
        metrics = original_apply(apply_session, results, **kwargs)
        seen["isolation"] = apply_session.scalar(text("SHOW transaction_isolation"))
        seen["row_locks"] = apply_session.scalar(
            text(
                "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                "WHERE c.relname = 'products' AND l.pid = pg_backend_pid() "
                "AND l.mode = 'RowShareLock'"
            )
        )
        return metrics

    monkeypatch.setattr(main_mod, "_apply_pharmonline_public_api_reconciliation", observing_apply)
    results = _catalog(known, _pg_api(tag, "n", "brand-new"))

    summary, admitted = main_mod._admit_pharmonline_public_api_scheduled_identities(
        session, results, tenant_id=tenant_id, run_id=run.id, source_transport="direct"
    )

    assert summary["status"] == "admitted", summary
    assert len(admitted) == 1
    assert seen == {"isolation": "serializable", "row_locks": 1}
    # Уровень изоляции не утекает в запись сбора, которая идёт следом.
    assert session.scalar(text("SHOW transaction_isolation")) == "read committed"
    session.rollback()
    (row,) = session.query(storage.PharmonlinePublicAPIIdentityAdmission).filter_by(
        tenant_id=tenant_id
    )
    assert (row.proof_version, row.source_transport, row.preflight_run_ref) == (
        SCHEDULED,
        "direct",
        str(run.id),
    )


def test_database_error_in_the_middle_of_a_batch_on_postgres(postgres_session):
    """PostgreSQL после ошибки прерывает транзакцию целиком: без отката на ней
    упала бы и проверка личностей, и запись сбора. SQLite это «прощает»."""
    from sqlalchemy import text

    session, tenant_id, tag = postgres_session
    run = storage.Run(status="running", tenant_id=tenant_id)
    session.add(run)
    known = _pg_api(tag, "k", "known")
    bystander = storage.Product(
        tenant_id=tenant_id,
        site="pharmonline",
        external_id=known.external_id,
        url=known.url,
        name=known.name,
        name_normalized=normalize_name(known.name),
        availability_source=DDP,
    )
    session.add(bystander)
    session.flush()
    colliding = _pg_api(tag, "c", "collides")
    # Старая непригодная строка журнала на идентификатор, который сайт назовёт новым.
    session.add(
        storage.PharmonlinePublicAPIIdentityAdmission(
            tenant_id=tenant_id,
            product_id=bystander.id,
            admission_kind="new_public_product",
            public_api_external_id=colliding.external_id,
            public_api_canonical_url=colliding.url,
            proof_version="stale_v0",
            source_manifest_sha256="a" * 64,
            catalog_fingerprint_sha256="b" * 64,
            source_transport="direct",
            preflight_run_ref="1",
        )
    )
    session.commit()
    results = _catalog(known, _pg_api(tag, "n", "brand-new"), colliding)

    summary, admitted = main_mod._admit_pharmonline_public_api_scheduled_identities(
        session, results, tenant_id=tenant_id, run_id=run.id, source_transport="direct"
    )

    assert summary["status"] == "refused"
    assert summary["reason"] == "apply_failed:IntegrityError"
    assert admitted == []
    # Сессия после отката рабочая, и от пачки ничего не осталось.
    assert (
        session.scalar(text("SELECT count(*) FROM products WHERE tenant_id = :t"), {"t": tenant_id})
        == 1
    )
    with pytest.raises(main_mod.PharmonlinePublicAPIIdentityError, match="missing_trusted_ids=2"):
        main_mod._verify_pharmonline_public_api_identities(session, results, tenant_id=tenant_id)
