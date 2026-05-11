"""Тесты CRUD для recipients и tracked products."""

from pathlib import Path

import pytest

from src import watchlist as wl


# === RECIPIENTS ===


def test_add_recipient_creates(db_session):
    r = wl.add_recipient(db_session, "client@pharmonline.az", name="Client")
    assert r.id is not None
    assert r.email == "client@pharmonline.az"
    assert r.is_active is True


def test_add_recipient_idempotent(db_session):
    """Повторное добавление того же email — обновляет, не дублирует."""
    a = wl.add_recipient(db_session, "x@y.com")
    b = wl.add_recipient(db_session, "x@y.com", name="Renamed")
    assert a.id == b.id
    assert b.name == "Renamed"


def test_add_recipient_normalizes_email(db_session):
    r = wl.add_recipient(db_session, "  CLIENT@PharmOnline.AZ  ")
    assert r.email == "client@pharmonline.az"


def test_remove_recipient(db_session):
    wl.add_recipient(db_session, "a@b.c")
    assert wl.remove_recipient(db_session, "a@b.c") is True
    assert wl.list_recipients(db_session) == []


def test_remove_nonexistent_returns_false(db_session):
    assert wl.remove_recipient(db_session, "nope@nope.com") is False


def test_toggle_recipient(db_session):
    wl.add_recipient(db_session, "x@y.com")
    r = wl.toggle_recipient(db_session, "x@y.com")
    assert r.is_active is False
    r = wl.toggle_recipient(db_session, "x@y.com")
    assert r.is_active is True


def test_active_recipients_only(db_session):
    wl.add_recipient(db_session, "active@x.com")
    wl.add_recipient(db_session, "inactive@x.com")
    wl.toggle_recipient(db_session, "inactive@x.com")
    assert wl.active_recipient_emails(db_session) == ["active@x.com"]


def test_update_recipient_changes_name(db_session):
    wl.add_recipient(db_session, "x@y.com", name="Old")
    r = wl.update_recipient(db_session, "x@y.com", name="New")
    assert r.name == "New"


# === WATCHLIST ===


def test_add_tracked_with_urls(db_session):
    tp = wl.add_tracked_product(
        db_session,
        "Paracetamol 500mg N20",
        brand="Bayer",
        dosage="500mg",
        pack_size="N20",
        pharmonline_url="https://www.pharmonline.az/product/123",
        aloe_url="https://aloe.az/product/abc",
    )
    assert tp.id is not None
    assert len(tp.links) == 3  # all 3 sites get a link entry
    sites = {link.site for link in tp.links}
    assert sites == {"pharmonline", "aptekonline", "aloe"}
    confirmed = [link for link in tp.links if link.status == "confirmed"]
    assert len(confirmed) == 2  # pharmonline + aloe URLs were given


def test_remove_tracked_cascades_links(db_session):
    tp = wl.add_tracked_product(db_session, "Foo", aloe_url="http://aloe.az/foo")
    tracked_id = tp.id
    assert wl.remove_tracked(db_session, tracked_id) is True
    assert wl.list_tracked(db_session, active_only=False) == []


def test_set_link_url_updates_existing(db_session):
    tp = wl.add_tracked_product(db_session, "Foo")
    link = wl.set_link_url(db_session, tp.id, "aloe", "https://aloe.az/x", status="confirmed")
    assert link.url == "https://aloe.az/x"
    assert link.status == "confirmed"


def test_set_link_url_unknown_site_raises(db_session):
    tp = wl.add_tracked_product(db_session, "Foo")
    with pytest.raises(ValueError):
        wl.set_link_url(db_session, tp.id, "unknown_site", "http://x.com")


def test_csv_roundtrip(db_session, tmp_path: Path):
    csv_path = tmp_path / "wl.csv"
    csv_path.write_text(
        "canonical_name,brand,dosage,pack_size,search_query,"
        "pharmonline_url,aptekonline_url,aloe_url,notes\n"
        "Paracetamol 500mg,Bayer,500mg,N20,paracetamol,"
        "https://ph.az/p1,,https://aloe.az/p1,top seller\n"
        "Aspirin,,,N30,aspirin,,,,\n",
        encoding="utf-8",
    )
    n = wl.import_from_csv(db_session, csv_path)
    assert n == 2

    items = wl.list_tracked(db_session)
    assert len(items) == 2
    paracetamol = next(t for t in items if t.canonical_name == "Paracetamol 500mg")
    urls = {link.site: link.url for link in paracetamol.links}
    assert urls.get("pharmonline") == "https://ph.az/p1"
    assert urls.get("aloe") == "https://aloe.az/p1"

    # экспорт обратно
    out_path = tmp_path / "out.csv"
    n_out = wl.export_to_csv(db_session, out_path)
    assert n_out == 2
    text = out_path.read_text(encoding="utf-8")
    assert "Paracetamol 500mg" in text
    assert "https://aloe.az/p1" in text
