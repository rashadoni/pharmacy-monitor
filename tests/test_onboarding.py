"""Тесты onboarding state-machine (src/onboarding.py).

Coverage gap: модуль был 0% покрыт. Все 4 state'а проверяем + is_complete().
"""

from __future__ import annotations

from datetime import datetime

from src import onboarding, storage


def test_empty_db_state(db_session):
    """Совсем пустая БД → state=empty_db."""
    s = onboarding.get_status(db_session)
    assert s.state == "empty_db"
    assert not s.has_runs
    assert not s.has_categories
    assert not s.has_recipients
    assert not s.has_products
    assert not s.has_tracked


def test_need_categories_state(db_session):
    """Run прошёл (есть Product), но Category/TrackedProduct нет → need_categories."""
    db_session.add(storage.Run(started_at=datetime.utcnow(), status="ok"))
    db_session.add(
        storage.Product(
            site="pharmonline",
            external_id="ext1",
            url="http://x",
            name="Test",
            name_normalized="test",
        )
    )
    db_session.commit()
    s = onboarding.get_status(db_session)
    assert s.state == "need_categories"
    assert s.has_runs
    assert s.has_products
    assert not s.has_categories
    assert not s.has_tracked


def test_need_recipients_state(db_session):
    """Categories есть, но Recipient нет → need_recipients."""
    db_session.add(storage.Run(started_at=datetime.utcnow(), status="ok"))
    db_session.add(
        storage.Product(
            site="pharmonline",
            external_id="ext2",
            url="http://x",
            name="Test",
            name_normalized="test",
        )
    )
    db_session.add(storage.Category(key="med", label_ru="Лекарства", label_az="Dərmanlar"))
    db_session.commit()
    s = onboarding.get_status(db_session)
    assert s.state == "need_recipients"
    assert s.has_categories
    assert not s.has_recipients


def test_ready_state(db_session):
    """Всё есть → ready."""
    db_session.add(storage.Run(started_at=datetime.utcnow(), status="ok"))
    db_session.add(
        storage.Product(
            site="pharmonline",
            external_id="ext3",
            url="http://x",
            name="Test",
            name_normalized="test",
        )
    )
    db_session.add(storage.Category(key="med", label_ru="Лекарства", label_az="Dərmanlar"))
    db_session.add(storage.Recipient(email="po@example.com", is_active=True))
    db_session.commit()
    s = onboarding.get_status(db_session)
    assert s.state == "ready"
    assert s.has_recipients


def test_inactive_recipient_does_not_count(db_session):
    """Recipient с is_active=False не считается."""
    db_session.add(storage.Run(started_at=datetime.utcnow(), status="ok"))
    db_session.add(
        storage.Product(
            site="pharmonline",
            external_id="ext4",
            url="http://x",
            name="Test",
            name_normalized="test",
        )
    )
    db_session.add(storage.Category(key="med", label_ru="Лекарства", label_az="Dərmanlar"))
    db_session.add(storage.Recipient(email="po@example.com", is_active=False))
    db_session.commit()
    s = onboarding.get_status(db_session)
    assert s.state == "need_recipients"


def test_tracked_product_satisfies_categories(db_session):
    """TrackedProduct наличие позволяет skip Category step."""
    db_session.add(storage.Run(started_at=datetime.utcnow(), status="ok"))
    db_session.add(
        storage.Product(
            site="pharmonline",
            external_id="ext5",
            url="http://x",
            name="Test",
            name_normalized="test",
        )
    )
    db_session.add(
        storage.TrackedProduct(
            tenant_id=1,
            canonical_name="Watched item",
            is_active=True,
        )
    )
    db_session.commit()
    s = onboarding.get_status(db_session)
    # has_categories=False но has_tracked=True → пропускаем need_categories,
    # сразу к need_recipients (нет Recipient)
    assert s.state == "need_recipients"
    assert s.has_tracked


def test_is_complete_helper(db_session):
    """is_complete() == True только когда state=ready."""
    assert onboarding.is_complete(db_session) is False
    # Setup ready state
    db_session.add(storage.Run(started_at=datetime.utcnow(), status="ok"))
    db_session.add(
        storage.Product(
            site="pharmonline",
            external_id="ready1",
            url="http://x",
            name="X",
            name_normalized="x",
        )
    )
    db_session.add(storage.Category(key="med", label_ru="Лекарства", label_az="Dərmanlar"))
    db_session.add(storage.Recipient(email="x@y", is_active=True))
    db_session.commit()
    assert onboarding.is_complete(db_session) is True
