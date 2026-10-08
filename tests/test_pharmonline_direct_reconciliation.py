"""Ручная сверка личностей pharmonline без Decodo — только простые допуски.

Зачем тест: с 2026-10-08 плановый сбор сам пишет допуски двух простых классов,
прочитав каталог напрямую, а ручная сверка — путь, который одобряет человек, —
ходила на сайт только через Decodo и строку с `direct` записать не могла. Decodo
бывает недоступен (2026-10-04 отвечал 407, 2026-10-06 план упал с
`decodo_request_failed`), и тогда всплеск простых товаров — больше потолка
автодопуска — разобрать было нечем.

Покрываем:
- отметка в журнале допусков — своя версия доказательства, только `direct` и
  только два простых класса; версия ручной сверки `direct` по-прежнему не
  принимает
- сверка напрямую допускает новый товар и товар, вернувшийся под тем же
  идентификатором и адресом, — без потолка автодопуска
- перекодировка, смена адреса (в том числе доказанная штрихкодом и названием
  или редиректом) и разведение с `direct` отказывают: и у самой записи, и в
  плане, до одобрения
- журналы перекодировок и нижней границы строку с `direct` не получают; нижнюю
  границу сверка напрямую не заводит и не двигает, а без неё отказывает
- сам скрипт сверки на настоящем PostgreSQL: план и запись через `direct`
  читают каталог по одному разу, запись отказывает, если её чтение разошлось с
  одобренным планом; через Decodo — по-прежнему два чтения подряд и прежняя
  версия
- код, который этой версии не знает, такие допуски не признаёт (так выглядит
  откат)

Чего тест не видит: настоящий сайт и его ограничение частоты запросов, сам
Decodo, шаги workflow (они — в tests/test_pharmonline_reconciliation_workflows.py).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import sessionmaker

import scripts.reconcile_pharmonline_public_api_identities as reconciliation
from src import main as main_mod
from src import storage
from src.scrapers.base import RouteStatus, ScrapeResult
from src.scrapers.pharmonline_public_api import PUBLIC_CATALOG_ROUTE
from tests.test_models_match_migrations import _admin
from tests.test_pharmonline_scheduled_admission import (
    MANUAL,
    SCHEDULED,
    _admissions,
    _admit,
    _api,
    _assert_nothing_written,
    _catalog,
    _meteor_id,
    _product_count,
    _stored,
    _url,
)

MANUAL_DIRECT = main_mod._PHARMONLINE_PUBLIC_API_MANUAL_DIRECT_ADMISSION_PROOF_VERSION
PLAN_RUN = "18353000001"  # номер прогона плана в GitHub Actions
PROXIED = ["crawlbase", "decodo", "scraperapi", "firecrawl"]
ReconciliationError = main_mod.PharmonlinePublicAPIReconciliationError


def _apply(db_session, results, **overrides):
    kwargs = dict(
        tenant_id=1,
        source_manifest_sha256="c" * 64,
        catalog_fingerprint_sha256=main_mod._pharmonline_public_api_catalog_fingerprint(results),
        source_transport="direct",
        preflight_run_ref=PLAN_RUN,
        admission_proof_version=MANUAL_DIRECT,
    )
    kwargs.update(overrides)
    return main_mod._apply_pharmonline_public_api_reconciliation(db_session, results, **kwargs)


def _baseline(floor: int, *, tenant_id: int = 1) -> storage.PharmonlinePublicAPICatalogBaseline:
    return storage.PharmonlinePublicAPICatalogBaseline(
        tenant_id=tenant_id,
        catalog_item_count=floor,
        minimum_catalog_item_count=floor,
        verified_identity_count=floor,
        trusted_ddp_item_count=floor,
        retired_ddp_item_count=0,
        reconciled_item_count=0,
        proof_version=main_mod._PHARMONLINE_PUBLIC_API_RECONCILIATION_PROOF_VERSION,
        source_manifest_sha256="a" * 64,
        catalog_fingerprint_sha256="b" * 64,
        source_transport="decodo",
        preflight_run_ref="17000000001",
    )


def _baseline_count(db_session) -> int:
    return db_session.query(storage.PharmonlinePublicAPICatalogBaseline).count()


def _admission_row(product: storage.Product, **overrides):
    values = dict(
        tenant_id=product.tenant_id,
        product_id=product.id,
        admission_kind="existing_native_id",
        public_api_external_id=str(product.external_id),
        public_api_canonical_url=product.url,
        proof_version=MANUAL_DIRECT,
        source_manifest_sha256="c" * 64,
        catalog_fingerprint_sha256="d" * 64,
        source_transport="direct",
        preflight_run_ref=PLAN_RUN,
    )
    values.update(overrides)
    return storage.PharmonlinePublicAPIIdentityAdmission(**values)


# ─── Отметка в журнале допусков ──────────────────────────────────────────────


def test_direct_proof_version_is_a_data_contract():
    """Строка ляжет в журнал допусков на проде. Переименовать её — значит
    объявить такие допуски недействительными."""
    assert MANUAL_DIRECT == "public_api_manual_direct_admission_v1"
    assert MANUAL_DIRECT not in {MANUAL, SCHEDULED}
    column = storage.PharmonlinePublicAPIIdentityAdmission.__table__.c.proof_version
    assert len(MANUAL_DIRECT) <= column.type.length
    transports, kinds = main_mod._PHARMONLINE_PUBLIC_API_ADMISSION_RULES_BY_PROOF[MANUAL_DIRECT]
    assert transports == {"direct"}
    assert kinds == {"new_public_product", "existing_native_id"}
    # Версии, с которыми запись не выполняет ничего, кроме простых допусков.
    assert main_mod._PHARMONLINE_PUBLIC_API_PLAIN_ADMISSION_PROOF_VERSIONS == {
        SCHEDULED,
        MANUAL_DIRECT,
    }


def test_manual_proof_version_still_has_no_direct_transport():
    """Новая версия не сняла прежнее правило: строка версии ручной сверки с
    `direct` по-прежнему значит «написано мимо кода»."""
    manual_transports, _ = main_mod._PHARMONLINE_PUBLIC_API_ADMISSION_RULES_BY_PROOF[MANUAL]
    assert "direct" not in manual_transports
    assert "direct" not in main_mod._PHARMONLINE_PUBLIC_API_WORKFLOW_PROOF_TRANSPORTS


@pytest.mark.parametrize(
    ("transport", "expected"),
    [("direct", MANUAL_DIRECT)] + [(name, MANUAL) for name in [*PROXIED, "", "made-up"]],
)
def test_transport_names_the_proof_version_of_a_manual_reconciliation(transport, expected):
    assert main_mod._pharmonline_public_api_manual_admission_proof_version(transport) == expected


# ─── Что сверка напрямую допускает ───────────────────────────────────────────


def test_direct_reconciliation_admits_new_and_returned_products(db_session):
    db_session.add_all([_stored(1, "known"), _stored(2, "returned", availability_source=None)])
    db_session.commit()
    results = _catalog(_api(1, "known"), _api(2, "returned"), _api(3, "brand-new"))

    metrics = _apply(db_session, results)
    db_session.commit()

    assert metrics["existing_native_admissions_ready"] == 1
    assert metrics["new_public_product_admissions_ready"] == 1
    rows = _admissions(db_session)
    assert sorted(row.admission_kind for row in rows) == [
        "existing_native_id",
        "new_public_product",
    ]
    for row in rows:
        # Отметка, по которой сверку напрямую видно в журнале: не версия ручной
        # сверки и не версия планового сбора.
        assert row.proof_version == MANUAL_DIRECT
        assert row.source_transport == "direct"
        assert row.preflight_run_ref == PLAN_RUN
        assert row.source_manifest_sha256 == "c" * 64
    assert _product_count(db_session) == 3
    assert db_session.query(storage.PharmonlinePublicAPIIdentityReconciliation).count() == 0
    assert db_session.query(storage.PharmonlinePublicAPIIdentityQuarantine).count() == 0
    # Каталог после этого проходит полную проверку личностей.
    assert main_mod._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1) == 3


def test_direct_reconciliation_takes_the_burst_the_scheduled_run_refuses(db_session, monkeypatch):
    """Ради этого путь и нужен: больше потолка автодопуска, а Decodo молчит."""
    monkeypatch.setenv(main_mod._PHARMONLINE_PUBLIC_API_SCHEDULED_ADMISSION_LIMIT_ENV, "3")
    db_session.add(_stored(1, "known"))
    db_session.commit()
    results = _catalog(_api(1, "known"), *[_api(10 + index, f"new-{index}") for index in range(5)])

    summary, admitted = _admit(db_session, results)

    assert summary["status"] == "refused"
    assert summary["reason"] == "limit_exceeded:candidates=5,limit=3"
    assert admitted == []
    _assert_nothing_written(db_session, products=1)

    _apply(db_session, results)
    db_session.commit()

    assert [row.proof_version for row in _admissions(db_session)] == [MANUAL_DIRECT] * 5
    assert main_mod._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1) == 6
    # Следующему плановому сбору допускать уже нечего.
    summary, admitted = _admit(db_session, results)
    assert (summary["status"], admitted) == ("nothing_to_admit", [])


# ─── Что с direct отказывает ─────────────────────────────────────────────────


def test_direct_reconciliation_refuses_a_legacy_rekey(db_session):
    db_session.add(
        _stored(1, "legacy-product", external_id="legacy-product", availability_source=None)
    )
    db_session.commit()

    with pytest.raises(ReconciliationError, match="direct reconciliation is limited to plain"):
        _apply(db_session, _catalog(_api(1, "legacy-product"), _api(2, "brand-new")))
    db_session.rollback()

    # Не записано ничего — и простой новый товар рядом тоже.
    _assert_nothing_written(db_session, products=1)
    assert db_session.query(storage.Product).one().external_id == "legacy-product"


def test_direct_reconciliation_refuses_an_address_change_proven_by_barcode_and_name(db_session):
    """Такую смену адреса сверка через Decodo пишет без запроса к сайту. Напрямую
    — всё равно нет: журнал перекодировок строку с `direct` не принимает."""
    db_session.add(_stored(1, "old-address", name="Aspirin 500", barcode="4770001112223"))
    db_session.commit()
    results = _catalog(_api(1, "new-address", name="Aspirin 500", barcode="4770001112223"))
    _actions, _admissions_plan, _quarantines, metrics = (
        main_mod._pharmonline_public_api_reconciliation_plan(db_session, results, tenant_id=1)
    )
    assert metrics["native_id_url_rebind_ready"] == 1, "состояние теста: смена адреса доказана"
    assert main_mod._pharmonline_public_api_reconciliation_is_safe(metrics)

    with pytest.raises(ReconciliationError, match="direct reconciliation is limited to plain"):
        _apply(db_session, results)
    db_session.rollback()

    _assert_nothing_written(db_session, products=1)
    assert db_session.query(storage.Product).one().url == _url("old-address")


def _moved(db_session):
    """Товар, у которого сайт сменил адрес, и доказательство редиректа для него."""
    stored = _stored(1, "old-address")
    db_session.add(stored)
    db_session.commit()
    results = _catalog(_api(1, "new-address", name="Other name"))
    proof = dict(
        product_id=stored.id,
        public_api_external_id=_meteor_id(1),
        legacy_canonical_url=_url("old-address"),
        public_api_canonical_url=_url("new-address"),
    )
    return results, proof


def test_direct_reconciliation_refuses_an_address_change_proven_by_redirect(db_session):
    """Доказательство редиректа добывает только Decodo; если оно всё же попало в
    вызов с `direct`, запись его не исполняет."""
    results, proof = _moved(db_session)
    redirect_proofs = (main_mod._PharmonlinePublicAPIRedirectProof(**proof),)
    _a, _b, _c, metrics = main_mod._pharmonline_public_api_reconciliation_plan(
        db_session, results, tenant_id=1, redirect_proofs=redirect_proofs
    )
    assert metrics["native_id_url_rebind_redirect_ready"] == 1, "состояние теста"

    with pytest.raises(ReconciliationError, match="direct reconciliation is limited to plain"):
        _apply(db_session, results, redirect_proofs=redirect_proofs)
    db_session.rollback()

    _assert_nothing_written(db_session, products=1)


def test_direct_reconciliation_refuses_an_identity_split(db_session):
    results, proof = _moved(db_session)
    split_proofs = (main_mod._PharmonlinePublicAPILegacySelfRedirectProof(**proof),)
    _a, _b, quarantines, metrics = main_mod._pharmonline_public_api_reconciliation_plan(
        db_session, results, tenant_id=1, legacy_self_redirect_proofs=split_proofs
    )
    assert (len(quarantines), metrics["identity_splits_ready"]) == (1, 1), "состояние теста"

    with pytest.raises(ReconciliationError, match="requires the Decodo transport"):
        _apply(db_session, results, legacy_self_redirect_proofs=split_proofs)
    db_session.rollback()

    _assert_nothing_written(db_session, products=1)
    assert db_session.query(storage.Product).one().external_id == _meteor_id(1)


def test_plain_only_proof_version_refuses_a_split_even_through_decodo(db_session):
    """Версия планового сбора ходит и через Decodo, а разведение через Decodo
    разрешено — ручной сверке. Версию «только простые допуски» это не касается."""
    results, proof = _moved(db_session)
    split_proofs = (main_mod._PharmonlinePublicAPILegacySelfRedirectProof(**proof),)

    with pytest.raises(ReconciliationError, match="scheduled admission is limited to plain"):
        _apply(
            db_session,
            results,
            legacy_self_redirect_proofs=split_proofs,
            source_transport="decodo",
            admission_proof_version=SCHEDULED,
        )
    db_session.rollback()

    _assert_nothing_written(db_session, products=1)
    assert db_session.query(storage.Product).one().external_id == _meteor_id(1)


def test_direct_reconciliation_refuses_an_unproven_address_change(db_session):
    results, _proof = _moved(db_session)

    with pytest.raises(ReconciliationError, match="native_id_url_rebind_unproven=1"):
        _apply(db_session, results)
    db_session.rollback()

    _assert_nothing_written(db_session, products=1)


@pytest.mark.parametrize("transport", PROXIED)
def test_direct_proof_version_is_refused_with_any_other_transport(db_session, transport):
    """Версия и транспорт идут парой в обе стороны: отметку сверки напрямую
    нельзя поставить на чтение через прокси."""
    with pytest.raises(ReconciliationError, match="requires valid immutable workflow evidence"):
        _apply(db_session, _catalog(_api(2, "brand-new")), source_transport=transport)

    _assert_nothing_written(db_session, products=0)


def test_direct_reconciliation_does_not_write_over_an_unusable_ledger_row(db_session):
    """Чего сверка напрямую не чинит (как и через Decodo): у товара уже лежит
    запись журнала допусков, которую проверка не признаёт. Вторая запись на тот
    же товар упирается в уникальный ключ — сверка падает, ничего не записав."""
    from sqlalchemy.exc import IntegrityError

    returned = _stored(2, "returned", availability_source=None)
    db_session.add_all([_stored(1, "known"), returned])
    db_session.commit()
    db_session.add(_admission_row(returned, proof_version="stale_v0"))
    db_session.commit()
    results = _catalog(_api(1, "known"), _api(2, "returned"), _api(3, "brand-new"))

    with pytest.raises(IntegrityError):
        _apply(db_session, results)
    db_session.rollback()

    assert [row.proof_version for row in _admissions(db_session)] == ["stale_v0"]
    assert _product_count(db_session) == 2


def test_plan_refusal_names_counts_only():
    """Строка идёт в журнал шага Actions, а он открыт: счётчики без товаров."""
    refusal = main_mod._pharmonline_public_api_plain_admission_refusal
    rekey = main_mod._PharmonlinePublicAPIReconciliationAction(
        product_id=7,
        legacy_external_id="secret-legacy-slug",
        public_api_external_id=_meteor_id(1),
        legacy_canonical_url=_url("secret-legacy-slug"),
        public_api_canonical_url=_url("secret-legacy-slug"),
        proof_version=main_mod._PHARMONLINE_PUBLIC_API_RECONCILIATION_PROOF_VERSION,
    )
    plain = main_mod._PharmonlinePublicAPIIdentityAdmissionAction(
        product_id=None,
        admission_kind="new_public_product",
        public_api_external_id=_meteor_id(2),
        public_api_canonical_url=_url("brand-new"),
        public_product=_api(2, "brand-new"),
    )
    split_kind = main_mod._PharmonlinePublicAPIIdentityAdmissionAction(
        product_id=None,
        admission_kind="quarantined_public_product",
        public_api_external_id=_meteor_id(3),
        public_api_canonical_url=_url("split"),
        public_product=_api(3, "split"),
    )

    for version in (MANUAL_DIRECT, SCHEDULED):
        assert refusal(version, [], [plain], []) is None
        assert refusal(version, [], [], []) is None
        assert (
            refusal(version, [rekey], [plain], [])
            == "identity_transitions=1, identity_splits=0, other_admission_kinds=0"
        )
        assert (
            refusal(version, [], [plain, split_kind], [object()])
            == "identity_transitions=0, identity_splits=1, other_admission_kinds=1"
        )
        # Каждая из трёх причин останавливает план и в одиночку.
        assert (
            refusal(version, [], [plain], [object()])
            == "identity_transitions=0, identity_splits=1, other_admission_kinds=0"
        )
        assert (
            refusal(version, [], [split_kind], [])
            == "identity_transitions=0, identity_splits=0, other_admission_kinds=1"
        )
        assert "secret" not in str(refusal(version, [rekey], [plain], []))
    # Версии ручной сверки через прокси можно всё.
    assert refusal(MANUAL, [rekey], [plain, split_kind], [object()]) is None


# ─── Чтение журналов: что считается доверенным ───────────────────────────────


def test_ledger_row_of_the_manual_version_with_direct_is_still_untrusted(db_session):
    returned = _stored(2, "returned", availability_source=None)
    db_session.add_all([_stored(1, "known"), returned])
    db_session.commit()
    row = _admission_row(returned, proof_version=MANUAL)
    db_session.add(row)
    db_session.commit()
    results = _catalog(_api(1, "known"), _api(2, "returned"))

    assert main_mod._pharmonline_public_api_admission_invalid_reason(row, returned) == "transport"
    with pytest.raises(main_mod.PharmonlinePublicAPIIdentityError, match="missing_trusted_ids=1"):
        main_mod._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, None),
        ({"admission_kind": "new_public_product"}, None),
        ({"source_transport": "decodo"}, "transport"),
        ({"source_transport": "firecrawl"}, "transport"),
        ({"admission_kind": "quarantined_public_product"}, "admission_kind"),
        ({"preflight_run_ref": "not-a-run"}, "preflight_run"),
    ],
)
def test_ledger_row_of_the_direct_version_is_read_by_its_own_rules(db_session, overrides, reason):
    returned = _stored(2, "returned", availability_source=None)
    db_session.add(returned)
    db_session.commit()
    row = _admission_row(returned, **overrides)

    assert main_mod._pharmonline_public_api_admission_invalid_reason(row, returned) == reason


def test_reconciliation_ledger_never_trusts_a_direct_row(db_session):
    """Журнал перекодировок и смен адреса остаётся за транспортами ручной сверки."""
    product = _stored(1, "new-address")
    db_session.add(product)
    db_session.commit()
    record = storage.PharmonlinePublicAPIIdentityReconciliation(
        tenant_id=1,
        product_id=product.id,
        legacy_external_id=_meteor_id(1),
        public_api_external_id=_meteor_id(1),
        legacy_canonical_url=_url("old-address"),
        public_api_canonical_url=_url("new-address"),
        proof_version=main_mod._PHARMONLINE_PUBLIC_API_REBIND_PROOF_VERSION,
        source_manifest_sha256="c" * 64,
        catalog_fingerprint_sha256="d" * 64,
        source_transport="decodo",
        preflight_run_ref=PLAN_RUN,
    )
    invalid_reason = main_mod._pharmonline_public_api_reconciliation_invalid_reason

    assert invalid_reason(record, product) is None, "состояние теста: через Decodo запись годна"
    record.source_transport = "direct"
    assert invalid_reason(record, product) == "transport"


def test_code_that_does_not_know_the_direct_version_distrusts_its_admissions(
    db_session, monkeypatch
):
    """Так выглядит откат: допуски сверки напрямую перестают признаваться, сбор
    отказывает, автодопуск поверх непризнанной записи не пишет. Данные целы."""
    db_session.add(_stored(1, "known"))
    db_session.commit()
    results = _catalog(_api(1, "known"), _api(2, "brand-new"))
    _apply(db_session, results)
    db_session.commit()
    assert main_mod._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1) == 2

    older_rules = dict(main_mod._PHARMONLINE_PUBLIC_API_ADMISSION_RULES_BY_PROOF)
    del older_rules[MANUAL_DIRECT]
    monkeypatch.setattr(main_mod, "_PHARMONLINE_PUBLIC_API_ADMISSION_RULES_BY_PROOF", older_rules)

    with pytest.raises(main_mod.PharmonlinePublicAPIIdentityError, match="missing_trusted_ids=1"):
        main_mod._verify_pharmonline_public_api_identities(db_session, results, tenant_id=1)
    summary, admitted = _admit(db_session, results)
    assert summary["status"] == "refused"
    assert summary["reason"] == "existing_identity_has_prior_audit=1"
    assert admitted == []
    assert [row.proof_version for row in _admissions(db_session)] == [MANUAL_DIRECT]


# ─── Нижняя граница каталога ─────────────────────────────────────────────────


def test_direct_reconciliation_requires_a_floor_it_did_not_create(db_session):
    results = _catalog(_api(1, "one"), _api(2, "two"), _api(3, "three"))

    with pytest.raises(ReconciliationError, match="requires an existing catalog floor"):
        main_mod._require_pharmonline_public_api_catalog_floor(db_session, results, tenant_id=1)

    assert _baseline_count(db_session) == 0


def test_direct_reconciliation_checks_the_existing_floor_without_writing(db_session):
    # Граница другого тенанта не в счёт; из двух своих действует большая.
    db_session.add_all([_baseline(2), _baseline(3), _baseline(900, tenant_id=2)])
    db_session.commit()
    require_floor = main_mod._require_pharmonline_public_api_catalog_floor
    three = _catalog(_api(1, "one"), _api(2, "two"), _api(3, "three"))
    two = _catalog(_api(1, "one"), _api(2, "two"))

    assert require_floor(db_session, three, tenant_id=1) == 3
    with pytest.raises(ReconciliationError, match="below its immutable recovery floor"):
        require_floor(db_session, two, tenant_id=1)

    db_session.rollback()
    assert _baseline_count(db_session) == 3
    assert not db_session.new and not db_session.dirty


@pytest.mark.parametrize("existing_floor", [None, 1])
def test_baseline_ledger_never_gets_a_direct_row(db_session, existing_floor):
    """Завести границу — и даже свериться с ней этой функцией — через `direct`
    нельзя: у сверки напрямую своя проверка, которая ничего не пишет."""
    if existing_floor is not None:
        db_session.add(_baseline(existing_floor))
        db_session.commit()
    results = _catalog(_api(1, "one"))

    with pytest.raises(ReconciliationError, match="requires valid immutable workflow evidence"):
        main_mod._ensure_pharmonline_public_api_catalog_baseline(
            db_session,
            results,
            tenant_id=1,
            verified_identity_count=1,
            trusted_ddp_item_count=0,
            retired_ddp_item_count=0,
            reconciled_item_count=0,
            source_manifest_sha256="c" * 64,
            catalog_fingerprint_sha256="d" * 64,
            source_transport="direct",
            preflight_run_ref=PLAN_RUN,
        )

    assert _baseline_count(db_session) == (0 if existing_floor is None else 1)


# ─── Сам скрипт сверки, на настоящем PostgreSQL ──────────────────────────────
#
# Скрипт берёт замок сбора, читает план в транзакции READ ONLY и пишет под
# SERIALIZABLE — на SQLite этого не исполнить. У каждого теста своя база:
# скрипт работает с тенантом 1 и читает все товары pharmonline.


def _postgres_server() -> URL:
    raw = os.environ.get("DATABASE_URL", "")
    if raw.startswith("postgresql"):
        return make_url(raw)
    if os.environ.get("CI"):
        pytest.fail(
            "в CI скрипт ручной сверки обязан исполняться на PostgreSQL, а DATABASE_URL — "
            "не он. Без этого запись допусков через direct не проверена ничем."
        )
    pytest.skip("нужен PostgreSQL в DATABASE_URL")


def _catalog_result(*products) -> ScrapeResult:
    count = len(products)
    return ScrapeResult(
        site="pharmonline",
        products=list(products),
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


class Site:
    """Сайт, как его видит скрипт: каждое чтение каталога — следующий ответ."""

    def __init__(self) -> None:
        self.catalogs: list[tuple] = []
        self.reads = 0
        self.transports: list[str] = []
        self.redirect_requests = 0

    def serve(self, *catalogs: tuple) -> None:
        self.catalogs = list(catalogs)

    def scraper_class(self):
        site = self

        class FakeScraper:
            async def __aenter__(self):
                site.transports.append(os.environ["PHARMONLINE_PUBLIC_API_TRANSPORT"])
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return None

            async def scrape(self, routes):
                assert routes == [PUBLIC_CATALOG_ROUTE]
                site.reads += 1
                # Последний ответ повторяется: каталог перестал меняться.
                products = site.catalogs.pop(0) if len(site.catalogs) > 1 else site.catalogs[0]
                return _catalog_result(*products)

            async def product_url_redirect_proof_reason(self, **_kwargs):
                site.redirect_requests += 1
                return "verified"

        return FakeScraper


@pytest.fixture
def stand(monkeypatch, tmp_path) -> Iterator["Stand"]:
    server = _postgres_server()
    name = f"pm_direct_reconcile_{uuid.uuid4().hex[:12]}"
    _admin(server, f'CREATE DATABASE "{name}"')
    url = server.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        storage.Base.metadata.create_all(engine)
        site = Site()
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setattr(reconciliation, "PharmonlinePublicAPIScraper", site.scraper_class())
        for variable in (
            "PHARMONLINE_PUBLIC_API_LOCK_WAIT_SECONDS",
            "PHARMONLINE_PUBLIC_API_SOURCE_MANIFEST_SHA256",
            "PHARMONLINE_PUBLIC_API_PREFLIGHT_RUN_REF",
            "PHARMONLINE_PUBLIC_API_EXPECTED_PLAN_CATALOG_FINGERPRINT_SHA256",
            "PHARMONLINE_PUBLIC_API_EXPECTED_PLAN_MANIFEST_SHA256",
            "PHARMONLINE_PUBLIC_API_EXPECTED_PLAN_PRODUCT_COUNT",
        ):
            monkeypatch.delenv(variable, raising=False)
        yield Stand(sessionmaker(engine, expire_on_commit=False), site, monkeypatch, tmp_path)
    finally:
        engine.dispose()
        cached = storage._SESSION_FACTORY_CACHE.pop(url, None)
        if cached is not None:
            cached.kw["bind"].dispose()
        _admin(server, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


class Stand:
    def __init__(self, session_factory, site: Site, monkeypatch, tmp_path) -> None:
        self.Session = session_factory
        self.site = site
        self.monkeypatch = monkeypatch
        self.evidence_path = tmp_path / "plan-evidence.json"

    def seed(self, *rows) -> None:
        with self.Session() as session:
            session.add_all(rows)
            session.commit()

    async def plan(self, transport: str) -> dict:
        self.evidence_path.unlink(missing_ok=True)
        self.monkeypatch.setenv("PHARMONLINE_PUBLIC_API_TRANSPORT", transport)
        self.monkeypatch.setenv(
            "PHARMONLINE_PUBLIC_API_PLAN_EVIDENCE_PATH", str(self.evidence_path)
        )
        await reconciliation.main(apply=False)
        return json.loads(self.evidence_path.read_text())

    async def apply(self, transport: str, evidence: dict) -> None:
        """Запись с тем, что workflow берёт из одобренного плана."""
        for variable, value in {
            "PHARMONLINE_PUBLIC_API_TRANSPORT": transport,
            "PHARMONLINE_PUBLIC_API_SOURCE_MANIFEST_SHA256": "c" * 64,
            "PHARMONLINE_PUBLIC_API_PREFLIGHT_RUN_REF": PLAN_RUN,
            "PHARMONLINE_PUBLIC_API_EXPECTED_PLAN_CATALOG_FINGERPRINT_SHA256": evidence[
                "catalog_fingerprint_sha256"
            ],
            "PHARMONLINE_PUBLIC_API_EXPECTED_PLAN_MANIFEST_SHA256": evidence[
                "candidate_manifest_sha256"
            ],
            "PHARMONLINE_PUBLIC_API_EXPECTED_PLAN_PRODUCT_COUNT": str(evidence["product_count"]),
        }.items():
            self.monkeypatch.setenv(variable, value)
        await reconciliation.main(apply=True)

    def state(self) -> dict:
        with self.Session() as session:
            return {
                "products": self._rows(
                    session, "SELECT external_id, url FROM products WHERE site = 'pharmonline'"
                ),
                "admissions": self._rows(
                    session,
                    "SELECT admission_kind, proof_version, source_transport, "
                    "preflight_run_ref FROM pharmonline_public_api_identity_admissions",
                ),
                "reconciliations": session.scalar(
                    text("SELECT count(*) FROM pharmonline_public_api_identity_reconciliations")
                ),
                "quarantines": session.scalar(
                    text("SELECT count(*) FROM pharmonline_public_api_identity_quarantines")
                ),
                "baselines": self._rows(
                    session,
                    "SELECT minimum_catalog_item_count, source_transport "
                    "FROM pharmonline_public_api_catalog_baselines",
                ),
            }

    @staticmethod
    def _rows(session, statement: str) -> list[tuple]:
        return sorted(tuple(row) for row in session.execute(text(statement)).all())


def _burst():
    """Каталог: один известный товар, один вернувшийся и три новых."""
    stored = [_stored(1, "known"), _stored(2, "returned", availability_source=None)]
    catalog = (
        _api(1, "known"),
        _api(2, "returned"),
        _api(3, "new-3"),
        _api(4, "new-4"),
        _api(5, "new-5"),
    )
    return stored, catalog


async def test_script_reconciles_plain_admissions_directly_with_one_read_each(stand: Stand):
    stored, catalog = _burst()
    stand.seed(*stored, _baseline(3))
    stand.site.serve(catalog)
    before = stand.state()

    evidence = await stand.plan("direct")

    # План ничего не пишет и читает каталог один раз.
    assert stand.site.reads == 1
    assert stand.state() == before
    assert evidence["transport"] == "direct"
    assert evidence["product_count"] == 5
    assert evidence["metrics"]["reconciliation_safe"] == 1
    assert evidence["metrics"]["new_public_product_admissions_ready"] == 3
    assert evidence["metrics"]["existing_native_admissions_ready"] == 1

    await stand.apply("direct", evidence)

    # Запись — второе чтение; вместе с чтением плана их два, и они совпали.
    assert stand.site.reads == 2
    assert stand.site.transports == ["direct", "direct"]
    assert stand.site.redirect_requests == 0
    after = stand.state()
    assert after["admissions"] == sorted(
        [("existing_native_id", MANUAL_DIRECT, "direct", PLAN_RUN)]
        + [("new_public_product", MANUAL_DIRECT, "direct", PLAN_RUN)] * 3
    )
    assert len(after["products"]) == 5
    assert (after["reconciliations"], after["quarantines"]) == (0, 0)
    # Нижняя граница — та же строка, что была: сверка напрямую её не пишет.
    assert after["baselines"] == before["baselines"] == [(3, "decodo")]
    with stand.Session() as session:
        assert (
            main_mod._verify_pharmonline_public_api_identities(
                session, [_catalog_result(*catalog)], tenant_id=1
            )
            == 5
        )


async def test_script_refuses_a_direct_apply_when_its_read_differs_from_the_plan(stand: Stand):
    """Второе одинаковое чтение не отменено, а перенесено: запись читает каталог
    сама и пишет, только если он тот же, что в одобренном плане."""
    stored, catalog = _burst()
    stand.seed(*stored, _baseline(3))
    stand.site.serve(catalog)
    evidence = await stand.plan("direct")
    before = stand.state()
    # Между планом и записью сайт выложил ещё один товар.
    stand.site.serve((*catalog, _api(6, "new-6")))

    with pytest.raises(SystemExit, match="fresh catalog differs from the approved read-only plan"):
        await stand.apply("direct", evidence)

    assert stand.state() == before


@pytest.mark.parametrize("case", ["legacy rekey", "address change by barcode and name"])
async def test_script_refuses_a_direct_plan_that_needs_an_identity_transition(
    stand: Stand, case: str
):
    """Отказ в плане, до одобрения: файла с планом, который можно было бы
    передать на запись, не остаётся."""
    if case == "legacy rekey":
        stored = [_stored(1, "legacy", external_id="legacy", availability_source=None)]
        catalog = (_api(1, "legacy"), _api(2, "brand-new"))
    else:
        stored = [_stored(1, "old-address", name="Aspirin 500", barcode="4770001112223")]
        catalog = (
            _api(1, "new-address", name="Aspirin 500", barcode="4770001112223"),
            _api(2, "brand-new"),
        )
    stand.seed(*stored, _baseline(1))
    stand.site.serve(catalog)
    before = stand.state()

    with pytest.raises(SystemExit) as refusal:
        await stand.plan("direct")

    message = str(refusal.value)
    assert "the direct transport admits plain identities only" in message
    assert "rerun the plan with the decodo transport" in message
    assert "identity_transitions=1, identity_splits=0" in message
    assert not stand.evidence_path.exists()
    assert stand.state() == before


async def test_script_refuses_a_direct_plan_with_an_unproven_address_change(stand: Stand):
    """Смену адреса без штрихкода и названия доказывает только редирект, а его
    умеет читать только Decodo: напрямую скрипт не делает ни одного запроса."""
    stand.seed(_stored(1, "old-address"), _baseline(1))
    stand.site.serve((_api(1, "new-address", name="Other name"),))
    before = stand.state()

    with pytest.raises(SystemExit, match="redirect proof requires the explicit Decodo transport"):
        await stand.plan("direct")

    assert stand.site.redirect_requests == 0
    assert not stand.evidence_path.exists()
    assert stand.state() == before


async def test_script_refuses_a_direct_plan_without_a_catalog_floor(stand: Stand):
    stored, catalog = _burst()
    stand.seed(*stored)
    stand.site.serve(catalog)

    with pytest.raises(
        SystemExit, match="direct reconciliation requires an existing catalog floor"
    ):
        await stand.plan("direct")

    assert not stand.evidence_path.exists()
    assert stand.state()["baselines"] == []


async def test_script_refuses_a_direct_plan_below_the_catalog_floor(stand: Stand):
    stored, catalog = _burst()
    stand.seed(*stored, _baseline(6))
    stand.site.serve(catalog)

    with pytest.raises(SystemExit, match="below its immutable recovery floor"):
        await stand.plan("direct")

    assert not stand.evidence_path.exists()


async def test_script_direct_apply_stops_at_the_floor_and_writes_nothing(stand: Stand):
    """План одобрен, а граница к записи оказалась выше каталога (или пропала):
    допуски, уже записанные в этой транзакции, откатываются."""
    stored, catalog = _burst()
    stand.seed(*stored, _baseline(3))
    stand.site.serve(catalog)
    evidence = await stand.plan("direct")
    stand.seed(_baseline(6))
    before = stand.state()

    with pytest.raises(ReconciliationError, match="below its immutable recovery floor"):
        await stand.apply("direct", evidence)

    assert stand.state() == before
    assert before["admissions"] == []


async def test_script_through_decodo_still_reads_twice_and_writes_the_manual_version(stand: Stand):
    """Путь через Decodo не изменился: два чтения подряд в плане и два в записи,
    прежняя версия доказательства, перекодировка разрешена, границу заводит он."""
    stand.seed(
        _stored(1, "known"),
        _stored(2, "legacy", external_id="legacy", availability_source=None),
    )
    # Нижняя граница первой сверки — не меньше 9000 товаров; каталог теста мал.
    stand.monkeypatch.setattr(main_mod, "_PHARMONLINE_PUBLIC_API_BOOTSTRAP_MIN_PRODUCTS", 2)
    catalog = (_api(1, "known"), _api(2, "legacy"), _api(3, "brand-new"))
    stand.site.serve(catalog)

    evidence = await stand.plan("decodo")
    assert stand.site.reads == 2
    assert evidence["transport"] == "decodo"
    assert evidence["metrics"]["legacy_rekeys_ready"] == 1

    await stand.apply("decodo", evidence)

    assert stand.site.reads == 4
    after = stand.state()
    assert after["admissions"] == [("new_public_product", MANUAL, "decodo", PLAN_RUN)]
    assert after["reconciliations"] == 1
    assert after["baselines"] == [(3, "decodo")]
    assert (_meteor_id(2), _url("legacy")) in after["products"]


async def test_script_through_decodo_refuses_two_reads_that_differ(stand: Stand):
    stored, catalog = _burst()
    stand.seed(*stored)
    stand.site.serve(catalog, (*catalog, _api(6, "new-6")))

    with pytest.raises(SystemExit, match="two complete source passes are not identical"):
        await stand.plan("decodo")

    assert stand.site.reads == 2
    assert not stand.evidence_path.exists()


async def test_script_cannot_apply_a_decodo_plan_with_transitions_directly(stand: Stand):
    """План одобрен через Decodo и содержит перекодировку; запись запущена с
    `direct`. Совпавшие отпечатки не помогают: запись отказывает."""
    stand.seed(
        _stored(1, "known"),
        _stored(2, "legacy", external_id="legacy", availability_source=None),
        _baseline(1),
    )
    stand.site.serve((_api(1, "known"), _api(2, "legacy")))
    evidence = await stand.plan("decodo")
    before = stand.state()

    with pytest.raises(ReconciliationError, match="direct reconciliation is limited to plain"):
        await stand.apply("direct", evidence)

    assert stand.state() == before
    assert before["reconciliations"] == 0
