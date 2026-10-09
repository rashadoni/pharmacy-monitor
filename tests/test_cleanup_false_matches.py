"""Tests for scripts/cleanup_false_matches.py (Phase 0.2).

Покрываем:
- CSV-парсинг (header validation, type coercion, error reporting)
- Идемпотентность (повторный прогон не дублирует MatchRejection)
- Корректная обработка edge-кейсов: продукт не в кластере, match не существует
- `cleanup` целиком на файловой базе: что из записанного с `--apply` доживает до
  новой сессии, что считает счётчик и что dry-run не пишет ничего
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cleanup_false_matches import _parse_csv, CleanupRow, cleanup  # noqa: E402
from src import match_actions, storage  # noqa: E402


def test_parse_csv_basic(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("match_id,detach_product_id,reason\n1,2,oops\n3,4,other\n", encoding="utf-8")
    rows = _parse_csv(p)
    assert rows == [
        CleanupRow(match_id=1, detach_product_id=2, reason="oops"),
        CleanupRow(match_id=3, detach_product_id=4, reason="other"),
    ]


def test_parse_csv_strips_whitespace_from_reason(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("match_id,detach_product_id,reason\n1,2,  whitespace around  \n", encoding="utf-8")
    rows = _parse_csv(p)
    assert rows[0].reason == "whitespace around"


def test_parse_csv_rejects_missing_column(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("match_id,detach_product_id\n1,2\n", encoding="utf-8")  # no `reason`
    with pytest.raises(ValueError, match="missing required columns"):
        _parse_csv(p)


def test_parse_csv_reports_line_number_on_bad_row(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("match_id,detach_product_id,reason\nNOT_A_NUMBER,2,oops\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        _parse_csv(p)


def test_break_match_idempotent_via_add_rejection(db_session):
    """add_rejection (used by break_match) is idempotent — repeat call returns same row."""
    s = db_session
    # Build a minimal cluster: 2 products → 1 Match.
    p_a = storage.Product(
        tenant_id=1,
        site="pharmonline",
        external_id="a",
        url="http://a",
        name="A",
        name_normalized="a",
    )
    p_b = storage.Product(
        tenant_id=1,
        site="aptekonline",
        external_id="b",
        url="http://b",
        name="B",
        name_normalized="b",
    )
    s.add_all([p_a, p_b])
    s.flush()
    m = storage.Match(tenant_id=1, canonical_name="canon")
    s.add(m)
    s.flush()
    p_a.canonical_id = m.id
    p_b.canonical_id = m.id
    s.commit()

    r1 = match_actions.add_rejection(s, p_a.id, p_b.id, reason="first")
    r2 = match_actions.add_rejection(s, p_a.id, p_b.id, reason="second")
    assert r1.id == r2.id  # same record returned, no duplicate
    # Reason of the first call is preserved (we don't overwrite on re-add).
    assert r1.reason == "first"


# --- cleanup() на файловой базе ---------------------------------------------
#
# `cleanup` открывает свою сессию по DATABASE_URL и закрывает её сам. Поэтому
# записанное проверяется так, как его увидит оператор после запуска: новой
# сессией на новом движке. Общая in-memory база из `db_session` тут не годится —
# в ней незакоммиченное видно всем.


@pytest.fixture
def cleanup_db(tmp_path: Path, monkeypatch) -> str:
    url = f"sqlite:///{tmp_path / 'cleanup.sqlite'}"
    engine = storage.make_engine(url)
    storage.Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def _in_new_session(url: str, read):
    """Выполнить чтение в сессии, которая не видела ничего незакоммиченного."""
    Session = storage.make_session(url)
    try:
        with Session() as s:
            return read(s)
    finally:
        Session.kw["bind"].dispose()


def _seed(url: str, *clusters: list[tuple[str, int]], loose: tuple[str, int] | None = None):
    """Завести кластеры и, если просят, товар без кластера.

    Товар задаётся парой (external_id, tenant_id). Возвращает id кластеров в
    порядке аргументов и словарь external_id → id товара.
    """
    sites = ("pharmonline", "aptekonline", "aloe")

    def write(s):
        product_ids: dict[str, int] = {}
        match_ids: list[int] = []
        for members in clusters:
            m = storage.Match(tenant_id=members[0][1], canonical_name="canon")
            s.add(m)
            s.flush()
            match_ids.append(m.id)
            for site, (ext, tenant_id) in zip(sites, members):
                p = _product(ext, site, tenant_id)
                p.canonical_id = m.id
                s.add(p)
                s.flush()
                product_ids[ext] = p.id
        if loose is not None:
            p = _product(loose[0], "aloe", loose[1])
            s.add(p)
            s.flush()
            product_ids[loose[0]] = p.id
        s.commit()
        return match_ids, product_ids

    return _in_new_session(url, write)


def _product(external_id: str, site: str, tenant_id: int) -> storage.Product:
    return storage.Product(
        tenant_id=tenant_id,
        site=site,
        external_id=external_id,
        url=f"http://{external_id}",
        name=external_id,
        name_normalized=external_id,
    )


def _write_csv(tmp_path: Path, *rows: tuple[int, int]) -> Path:
    p = tmp_path / "false_matches.csv"
    lines = ["match_id,detach_product_id,reason"]
    lines += [f"{match_id},{product_id},разные товары" for match_id, product_id in rows]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _committed_pairs(url: str) -> set[tuple[int, int]]:
    return _in_new_session(
        url,
        lambda s: {
            (a, b)
            for a, b in s.execute(
                select(storage.MatchRejection.product_a_id, storage.MatchRejection.product_b_id)
            )
        },
    )


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def test_cleanup_commits_rejections_of_already_detached_row(cleanup_db, tmp_path, capsys):
    """Единственная строка CSV — товар, уже отвязанный от кластера.

    До break_match такая строка не доходит, а коммитил только он: отказы
    делали flush и пропадали при закрытии сессии, хотя скрипт о них отчитывался.
    """
    (match_id,), ids = _seed(cleanup_db, [("a", 1), ("b", 1)], loose=("gone", 1))
    csv_path = _write_csv(tmp_path, (match_id, ids["gone"]))

    written = cleanup(csv_path, apply=True)

    assert written == 2
    assert "rejections_written=2" in capsys.readouterr().out
    assert _committed_pairs(cleanup_db) == {
        _pair(ids["gone"], ids["a"]),
        _pair(ids["gone"], ids["b"]),
    }


@pytest.mark.parametrize("detached_row_first", [True, False])
def test_cleanup_commits_already_detached_row_wherever_it_stands(
    cleanup_db, tmp_path, detached_row_first
):
    """Отказы строки «уже отвязан» не зависят от того, что идёт после неё.

    Раньше они сохранялись, только если следом шла строка, дошедшая до
    break_match: его коммит заодно фиксировал и их. Последняя такая строка
    терялась.
    """
    (m_detached, m_break), ids = _seed(
        cleanup_db,
        [("a", 1), ("b", 1)],
        [("x", 1), ("y", 1), ("z", 1)],
        loose=("gone", 1),
    )
    detached_row = (m_detached, ids["gone"])
    break_row = (m_break, ids["x"])
    rows = (detached_row, break_row) if detached_row_first else (break_row, detached_row)

    written = cleanup(_write_csv(tmp_path, *rows), apply=True)

    assert written == 4
    assert _committed_pairs(cleanup_db) == {
        _pair(ids["gone"], ids["a"]),
        _pair(ids["gone"], ids["b"]),
        _pair(ids["x"], ids["y"]),
        _pair(ids["x"], ids["z"]),
    }
    # Обычный случай не изменился: товар отвязан, кластер из двух оставшихся жив.
    members = _in_new_session(
        cleanup_db,
        lambda s: set(
            s.scalars(
                select(storage.Product.external_id).where(storage.Product.canonical_id == m_break)
            )
        ),
    )
    assert members == {"y", "z"}


def test_cleanup_does_not_count_cross_tenant_pairs(cleanup_db, tmp_path, capsys):
    """Для пары из разных тенантов add_rejection записи не создаёт — и считать нечего."""
    (match_id,), ids = _seed(cleanup_db, [("a", 1), ("b", 1)], loose=("foreign", 2))
    csv_path = _write_csv(tmp_path, (match_id, ids["foreign"]))

    written = cleanup(csv_path, apply=True)

    assert written == 0
    assert "rejections_written=0" in capsys.readouterr().out
    assert _committed_pairs(cleanup_db) == set()


def test_cleanup_dry_run_writes_nothing(cleanup_db, tmp_path):
    """Без --apply обе ветки только печатают: ни отказов, ни отвязки."""
    (m_detached, m_break), ids = _seed(
        cleanup_db,
        [("a", 1), ("b", 1)],
        [("x", 1), ("y", 1)],
        loose=("gone", 1),
    )
    csv_path = _write_csv(tmp_path, (m_detached, ids["gone"]), (m_break, ids["x"]))

    assert cleanup(csv_path, apply=False) == 0

    assert _committed_pairs(cleanup_db) == set()
    still_matched = _in_new_session(
        cleanup_db,
        lambda s: {
            ext: canonical_id
            for ext, canonical_id in s.execute(
                select(storage.Product.external_id, storage.Product.canonical_id)
            )
        },
    )
    assert still_matched == {
        "a": m_detached,
        "b": m_detached,
        "x": m_break,
        "y": m_break,
        "gone": None,
    }


def test_cleanup_rerun_with_same_csv_adds_no_rows(cleanup_db, tmp_path):
    """Повторный запуск попадает в ветку «уже отвязан»: записей не прибавляется."""
    (match_id,), ids = _seed(cleanup_db, [("x", 1), ("y", 1), ("z", 1)])
    csv_path = _write_csv(tmp_path, (match_id, ids["x"]))

    cleanup(csv_path, apply=True)
    after_first = _committed_pairs(cleanup_db)
    cleanup(csv_path, apply=True)

    assert after_first == {_pair(ids["x"], ids["y"]), _pair(ids["x"], ids["z"])}
    assert _committed_pairs(cleanup_db) == after_first
    rows = _in_new_session(
        cleanup_db, lambda s: len(s.scalars(select(storage.MatchRejection.id)).all())
    )
    assert rows == 2


def test_cleanup_counts_confirmed_pairs_per_row_not_new_records(cleanup_db, tmp_path, capsys):
    """`rejections_written` — подтверждённые пары по строкам, а не новые записи.

    Строка добавляет в счёт пары «её товар — участник кластера на момент
    строки», по которым отказ в силе, в том числе уже существовавшие. Здесь
    кластер из трёх и одна строка: её повтор в файле и повторный запуск считают
    те же две пары ещё раз, записей не добавляя. Так задумано: ноль значил бы
    «ничего не защищено», а не «всё уже было».
    """
    (match_id,), ids = _seed(cleanup_db, [("x", 1), ("y", 1), ("z", 1)])
    row = (match_id, ids["x"])

    assert cleanup(_write_csv(tmp_path, row, row), apply=True) == 4
    assert cleanup(_write_csv(tmp_path, row), apply=True) == 2

    out = capsys.readouterr().out
    assert "rejections_written=4 matches_dissolved=0 skipped=0" in out
    assert "rejections_written=2 matches_dissolved=0 skipped=0" in out
    assert len(_committed_pairs(cleanup_db)) == 2


def test_cleanup_reactivates_a_resolved_rejection(cleanup_db, tmp_path):
    """Снятый раньше отказ строка «уже отвязан» включает снова — и это сохраняется."""
    (match_id,), ids = _seed(cleanup_db, [("a", 1), ("b", 1)], loose=("gone", 1))

    def resolve(s):
        rejection = match_actions.add_rejection(s, ids["gone"], ids["a"], reason="old")
        rejection.is_active = False
        s.commit()

    _in_new_session(cleanup_db, resolve)

    cleanup(_write_csv(tmp_path, (match_id, ids["gone"])), apply=True)

    active = _in_new_session(
        cleanup_db,
        lambda s: {
            (a, b): is_active
            for a, b, is_active in s.execute(
                select(
                    storage.MatchRejection.product_a_id,
                    storage.MatchRejection.product_b_id,
                    storage.MatchRejection.is_active,
                )
            )
        },
    )
    assert active == {
        _pair(ids["gone"], ids["a"]): True,
        _pair(ids["gone"], ids["b"]): True,
    }


def test_cleanup_rejects_only_against_current_members(cleanup_db, tmp_path):
    """Кластер, из которого этот же запуск уже отвязал товар.

    Отказы пишутся с теми, кто в кластере сейчас, а не с тем, кого запуск уже
    отвязал: отказ с ним навсегда запретил бы свести два товара, которых в
    кластере нет.
    """
    (match_id,), ids = _seed(cleanup_db, [("x", 1), ("y", 1), ("z", 1)], loose=("gone", 1))
    csv_path = _write_csv(tmp_path, (match_id, ids["x"]), (match_id, ids["gone"]))

    written = cleanup(csv_path, apply=True)

    assert written == 4
    assert _committed_pairs(cleanup_db) == {
        _pair(ids["x"], ids["y"]),
        _pair(ids["x"], ids["z"]),
        _pair(ids["gone"], ids["y"]),
        _pair(ids["gone"], ids["z"]),
    }


def test_cleanup_survives_the_same_row_twice(cleanup_db, tmp_path):
    """Повтор строки в одном файле — не «отказ товара с самим собой» и не обрыв запуска."""
    (match_id,), ids = _seed(cleanup_db, [("x", 1), ("y", 1), ("z", 1)])
    csv_path = _write_csv(tmp_path, (match_id, ids["x"]), (match_id, ids["x"]))

    cleanup(csv_path, apply=True)

    assert _committed_pairs(cleanup_db) == {
        _pair(ids["x"], ids["y"]),
        _pair(ids["x"], ids["z"]),
    }


def _clusters(url: str) -> tuple[dict[str, int | None], set[int]]:
    """Привязки товаров (external_id → кластер) и id живых кластеров."""
    return _in_new_session(
        url,
        lambda s: (
            {
                ext: canonical_id
                for ext, canonical_id in s.execute(
                    select(storage.Product.external_id, storage.Product.canonical_id)
                )
            },
            set(s.scalars(select(storage.Match.id))),
        ),
    )


@pytest.mark.parametrize("detached_row_between", [False, True])
def test_cleanup_two_rows_of_one_cluster_dissolve_it(
    cleanup_db, tmp_path, capsys, detached_row_between
):
    """Пример из шапки скрипта: две строки на один кластер из трёх товаров.

    Вторая отвязка видела в сессии состав до первой: писала отказ с уже
    отвязанным товаром и оставляла кластер из одного товара. Итог не должен
    зависеть и от того, стоит ли между строками строка «уже отвязан» — она
    перечитывает состав сама.
    """
    (match_id,), ids = _seed(cleanup_db, [("x", 1), ("y", 1), ("z", 1)], loose=("gone", 1))
    rows = [(match_id, ids["x"]), (match_id, ids["y"])]
    expected = {_pair(ids["x"], ids["y"]), _pair(ids["x"], ids["z"]), _pair(ids["y"], ids["z"])}
    if detached_row_between:
        rows.insert(1, (match_id, ids["gone"]))
        expected |= {_pair(ids["gone"], ids["y"]), _pair(ids["gone"], ids["z"])}

    written = cleanup(_write_csv(tmp_path, *rows), apply=True)

    out = capsys.readouterr().out
    assert _clusters(cleanup_db) == ({"x": None, "y": None, "z": None, "gone": None}, set())
    assert _committed_pairs(cleanup_db) == expected
    # Отчёт совпадает с базой: каждый отказ посчитан один раз, кластер распущен.
    assert written == len(expected)
    assert f"rejections_written={len(expected)} matches_dissolved=1 skipped=0" in out
    # Вторая строка печатает кластер таким, каким он стал после первой.
    assert f"detach product_id={ids['y']} (cluster size 2)" in out


def test_cleanup_skips_a_row_of_a_cluster_this_run_dissolved(cleanup_db, tmp_path, capsys):
    """Три строки на кластер из трёх: вторая его распускает, третьей делать нечего.

    Раньше третья отвязывала последний товар от кластера, которого не должно
    было остаться, — и оставляла в базе кластер без единого товара.
    """
    (match_id,), ids = _seed(cleanup_db, [("x", 1), ("y", 1), ("z", 1)])
    csv_path = _write_csv(
        tmp_path, (match_id, ids["x"]), (match_id, ids["y"]), (match_id, ids["z"])
    )

    written = cleanup(csv_path, apply=True)

    out = capsys.readouterr().out
    assert _clusters(cleanup_db) == ({"x": None, "y": None, "z": None}, set())
    assert written == len(_committed_pairs(cleanup_db)) == 3
    assert f"SKIP: match_id={match_id} not found" in out
    assert "rejections_written=3 matches_dissolved=1 skipped=1" in out


def test_cleanup_keeps_already_detached_row_when_a_later_row_fails(
    cleanup_db, tmp_path, monkeypatch
):
    """Строка «уже отвязан» сохраняется сразу, как обычная: обрыв запуска на
    следующей строке её отказы не уносит."""
    (m_detached, m_break), ids = _seed(
        cleanup_db,
        [("a", 1), ("b", 1)],
        [("x", 1), ("y", 1)],
        loose=("gone", 1),
    )
    csv_path = _write_csv(tmp_path, (m_detached, ids["gone"]), (m_break, ids["x"]))

    def connection_lost(*args, **kwargs):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(match_actions, "break_match", connection_lost)
    with pytest.raises(RuntimeError, match="connection lost"):
        cleanup(csv_path, apply=True)

    assert _committed_pairs(cleanup_db) == {
        _pair(ids["gone"], ids["a"]),
        _pair(ids["gone"], ids["b"]),
    }
