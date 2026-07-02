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


# === CATEGORIES ===


def test_add_category_creates(db_session):
    c = wl.add_category(db_session, key="vitamins", label_ru="Витамины")
    assert c.id is not None
    assert c.key == "vitamins"
    assert c.label_ru == "Витамины"
    assert c.is_active is True


def test_add_category_normalizes_key(db_session):
    """key trim'ится + lowercase."""
    c = wl.add_category(db_session, key=" VITAMINS ", label_ru="V")
    assert c.key == "vitamins"


def test_add_category_idempotent_updates(db_session):
    a = wl.add_category(db_session, key="med", label_ru="Лекарства")
    b = wl.add_category(db_session, key="med", label_ru="Drugs", pharmonline_slug="meds")
    assert a.id == b.id
    assert b.label_ru == "Drugs"
    assert b.pharmonline_slug == "meds"


def test_list_categories_active_only(db_session):
    wl.add_category(db_session, key="a", label_ru="A", is_active=True)
    wl.add_category(db_session, key="b", label_ru="B", is_active=False)
    all_cats = wl.list_categories(db_session)
    active = wl.list_categories(db_session, active_only=True)
    assert len(all_cats) == 2
    assert len(active) == 1
    assert active[0].key == "a"


def test_get_category_returns_none_for_missing(db_session):
    assert wl.get_category(db_session, "nope") is None


def test_update_category_modifies_fields(db_session):
    c = wl.add_category(db_session, key="x", label_ru="X")
    updated = wl.update_category(db_session, c.id, label_ru="X-NEW", pharmonline_slug="ph-x")
    assert updated.label_ru == "X-NEW"
    assert updated.pharmonline_slug == "ph-x"


def test_update_category_empty_slug_becomes_none(db_session):
    """Пустая строка для slug-полей → None (явное удаление маппинга)."""
    c = wl.add_category(db_session, key="x", label_ru="X", pharmonline_slug="phx")
    updated = wl.update_category(db_session, c.id, pharmonline_slug="")
    assert updated.pharmonline_slug is None


def test_update_category_unknown_returns_none(db_session):
    assert wl.update_category(db_session, 9999, label_ru="x") is None


def test_remove_category(db_session):
    c = wl.add_category(db_session, key="x", label_ru="X")
    assert wl.remove_category(db_session, c.id) is True
    assert wl.get_category(db_session, "x") is None


def test_remove_category_unknown_returns_false(db_session):
    assert wl.remove_category(db_session, 9999) is False


def test_toggle_category_flips_active(db_session):
    c = wl.add_category(db_session, key="x", label_ru="X", is_active=True)
    flipped = wl.toggle_category(db_session, c.id)
    assert flipped.is_active is False
    flipped = wl.toggle_category(db_session, c.id)
    assert flipped.is_active is True


def test_toggle_category_unknown_returns_none(db_session):
    assert wl.toggle_category(db_session, 9999) is None


def test_categories_for_site_returns_only_active_with_slug(db_session):
    """Active=True + non-null site_slug → попадает в список."""
    wl.add_category(
        db_session,
        key="a",
        label_ru="A",
        pharmonline_slug="phA",
        aloe_slug="alA",
        is_active=True,
    )
    wl.add_category(
        db_session,
        key="b",
        label_ru="B",
        pharmonline_slug="phB",
        is_active=False,  # inactive — игнор
    )
    wl.add_category(
        db_session,
        key="c",
        label_ru="C",
        aloe_slug="alC",  # нет pharmonline_slug
        is_active=True,
    )
    assert sorted(wl.categories_for_site(db_session, "pharmonline")) == ["phA"]
    assert sorted(wl.categories_for_site(db_session, "aloe")) == ["alA", "alC"]


def test_add_tracked_category_idempotent(db_session):
    cat = wl.add_category(db_session, key="vit", label_ru="Витамины", pharmonline_slug="vit")
    first = wl.add_tracked_category(db_session, cat.id, tenant_id=1, notes="top")
    second = wl.add_tracked_category(db_session, cat.id, tenant_id=1, notes="updated")
    assert first.id == second.id
    assert second.notes == "updated"
    rows = wl.list_tracked_categories(db_session, tenant_id=1)
    assert len(rows) == 1
    assert rows[0].category.key == "vit"


def test_remove_tracked_category_scoped_by_tenant(db_session):
    cat = wl.add_category(db_session, key="baby", label_ru="Детский мир")
    tracked = wl.add_tracked_category(db_session, cat.id, tenant_id=2)
    assert wl.remove_tracked_category(db_session, tracked.id, tenant_id=1) is False
    assert wl.remove_tracked_category(db_session, tracked.id, tenant_id=2) is True
    assert wl.list_tracked_categories(db_session, tenant_id=2, active_only=False) == []


def test_categories_for_site_filters_by_category_id(db_session):
    a = wl.add_category(db_session, key="a", label_ru="A", pharmonline_slug="phA")
    wl.add_category(db_session, key="b", label_ru="B", pharmonline_slug="phB")
    assert wl.categories_for_site(db_session, "pharmonline", only_category_id=a.id) == ["phA"]


def test_categories_for_site_unknown_site_returns_empty(db_session):
    wl.add_category(db_session, key="a", label_ru="A", pharmonline_slug="phA", is_active=True)
    assert wl.categories_for_site(db_session, "not-a-real-site") == []


def test_seed_categories_from_yaml(db_session, tmp_path: Path):
    """YAML seed создаёт категории."""
    yaml_path = tmp_path / "cats.yaml"
    yaml_path.write_text(
        """
categories:
  vitamins:
    label_ru: Витамины
    label_az: Vitaminlər
    pharmonline: vitamins
    aptekonline: vitamins-ap
    aloe: vitamins-al
  cosmetics:
    label_ru: Косметика
    pharmonline: cosmetics
""",
        encoding="utf-8",
    )
    n = wl.seed_categories_from_yaml(db_session, yaml_path)
    assert n == 2
    cats = wl.list_categories(db_session)
    assert {c.key for c in cats} == {"vitamins", "cosmetics"}
    v = wl.get_category(db_session, "vitamins")
    assert v.pharmonline_slug == "vitamins"
    assert v.label_az == "Vitaminlər"


def test_seed_categories_missing_yaml_returns_zero(db_session, tmp_path: Path):
    """Несуществующий yaml → 0, не падает."""
    n = wl.seed_categories_from_yaml(db_session, tmp_path / "nope.yaml")
    assert n == 0


def test_seed_categories_idempotent_on_re_run(db_session, tmp_path: Path):
    """Второй seed обновляет, не дублирует."""
    yaml_path = tmp_path / "cats.yaml"
    yaml_path.write_text("categories:\n  v:\n    label_ru: Vitamins\n", encoding="utf-8")
    wl.seed_categories_from_yaml(db_session, yaml_path)
    yaml_path.write_text("categories:\n  v:\n    label_ru: Витамины (updated)\n", encoding="utf-8")
    wl.seed_categories_from_yaml(db_session, yaml_path)
    cats = wl.list_categories(db_session)
    assert len(cats) == 1
    assert cats[0].label_ru == "Витамины (updated)"


# === SAVED VIEWS ===


def test_upsert_saved_view_creates(db_session):
    v = wl.upsert_saved_view(
        db_session, name="My View", params={"min_sites": 2, "search": "nestle"}
    )
    assert v.id is not None
    assert v.name == "My View"
    assert v.params["search"] == "nestle"


def test_upsert_saved_view_updates_existing(db_session):
    wl.upsert_saved_view(db_session, name="V1", params={"a": 1})
    v2 = wl.upsert_saved_view(db_session, name="V1", params={"a": 2}, scope="other")
    assert v2.params == {"a": 2}
    assert v2.scope == "other"
    # Не дублируется
    assert len(wl.list_saved_views(db_session)) == 1


def test_list_saved_views_scope_filter(db_session):
    wl.upsert_saved_view(db_session, "a", {"x": 1}, scope="comparison")
    wl.upsert_saved_view(db_session, "b", {"y": 2}, scope="other")
    all_views = wl.list_saved_views(db_session)
    filtered = wl.list_saved_views(db_session, scope="comparison")
    assert len(all_views) == 2
    assert len(filtered) == 1
    assert filtered[0].name == "a"


def test_get_saved_view_returns_none_for_missing(db_session):
    assert wl.get_saved_view(db_session, "nope") is None


def test_delete_saved_view(db_session):
    wl.upsert_saved_view(db_session, "X", {"a": 1})
    assert wl.delete_saved_view(db_session, "X") is True
    assert wl.get_saved_view(db_session, "X") is None


def test_delete_saved_view_unknown_returns_false(db_session):
    assert wl.delete_saved_view(db_session, "nope") is False
