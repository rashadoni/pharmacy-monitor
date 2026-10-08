"""Bulk-reject known false matches via CSV input.

Phase 0.2 (2026-05-26). Семь известных false matches (Friso 3 Gold ↔ Friso
Prematures и подобные) сидят в проде с момента запуска матчера. UI-кнопка
"✗ Не один товар" работает, но кликать каждую вручную — задача на 15 минут;
скрипт делает то же массово и идемпотентно.

CSV формат (с заголовком):

    match_id,detach_product_id,reason
    142,9871,Friso 3 Gold vs Friso Prematures — разные продукты
    142,9882,Friso 3 Gold vs Friso Prematures — разные продукты
    198,5577,manual override

Для каждой строки:
    1. Создаёт MatchRejection (idempotent — если пара уже отрицалась, возвращает
       существующую запись без дубликата)
    2. Снимает canonical_id с detach_product (через src.match_actions.break_match)
    3. Если в кластере остался < 2 продуктов — Match удаляется целиком

Идемпотентность: повторный запуск с тем же CSV безопасен (rejection уже есть,
detach сходит в no-op).

`rejections_written` в итоговой строке — не число новых записей. Каждая строка
CSV добавляет в него пары «её товар — участник кластера на момент этой строки»,
по которым отказ в силе: созданные, включённые снова и уже существовавшие
(такая запись подтверждается). Поэтому повтор строки в файле и повторный запуск
могут посчитать уже записанные пары ещё раз, не добавив ни одной записи. Строки
кластера, которого уже нет, идут в `skipped`.

Usage:
    # Dry-run — печатает что будет сделано:
    python -m scripts.cleanup_false_matches data/false_matches.csv

    # Применить:
    python -m scripts.cleanup_false_matches data/false_matches.csv --apply

    # С другим DB (по умолчанию читает DATABASE_URL из env):
    DATABASE_URL=postgresql+psycopg://pm:PWD@host/pharmacy_monitor \\
        python -m scripts.cleanup_false_matches data/false_matches.csv --apply
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Allow running both `python scripts/cleanup_false_matches.py` and `python -m`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import match_actions, storage  # noqa: E402


@dataclass
class CleanupRow:
    match_id: int
    detach_product_id: int
    reason: str


def _parse_csv(path: Path) -> list[CleanupRow]:
    """Read CSV with `match_id,detach_product_id,reason` columns."""
    rows: list[CleanupRow] = []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"match_id", "detach_product_id", "reason"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"CSV missing required columns: {sorted(missing)}. Header was: {reader.fieldnames}"
            )
        for i, raw in enumerate(reader, start=2):  # line 1 = header
            try:
                rows.append(
                    CleanupRow(
                        match_id=int(raw["match_id"]),
                        detach_product_id=int(raw["detach_product_id"]),
                        reason=(raw["reason"] or "").strip(),
                    )
                )
            except (KeyError, ValueError) as exc:
                raise ValueError(f"line {i}: bad row {raw!r}: {exc}") from exc
    return rows


def cleanup(csv_path: Path, apply: bool = False) -> int:
    """Apply cleanup. Returns `rejections_written` (confirmed pairs, see module docstring)."""
    rows = _parse_csv(csv_path)
    if not rows:
        print(f"CSV {csv_path} has 0 data rows. Nothing to do.")
        return 0

    print(f"Loaded {len(rows)} row(s) from {csv_path}.")
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        return -1

    Session = storage.make_session(db_url)
    rejections_written = 0
    matches_dissolved = 0
    skipped = 0

    with Session() as db:
        for r in rows:
            match = db.get(storage.Match, r.match_id)
            if match is None:
                print(f"  SKIP: match_id={r.match_id} not found")
                skipped += 1
                continue
            detach = db.get(storage.Product, r.detach_product_id)
            if detach is None:
                print(f"  SKIP: product_id={r.detach_product_id} not found")
                skipped += 1
                continue
            if detach.canonical_id != r.match_id:
                # Уже не в этом кластере — возможно повторный запуск, или человек
                # сделал то же через UI. Запишем явный rejection на всякий случай.
                print(
                    f"  NOTE: product_id={r.detach_product_id} canonical_id"
                    f"={detach.canonical_id} != match_id={r.match_id} (already detached?)"
                )
                if apply:
                    # Создадим rejection с каждым из текущих участников кластера.
                    # Список перечитываем: сессия без expire_on_commit, и после
                    # break_match в этом же запуске в нём остаётся уже отвязанный
                    # товар — отказ записался бы и с ним (а с самим собой — упал).
                    db.expire(match, ["products"])
                    # Для пары из разных тенантов add_rejection записи не создаёт
                    # и возвращает None — такую пару не считаем.
                    for other in match.products:
                        if match_actions.add_rejection(
                            db, r.detach_product_id, other.id, reason=r.reason or "bulk_cleanup"
                        ):
                            rejections_written += 1
                    # add_rejection делает только flush. Коммитим строку сразу, как
                    # break_match коммитит обычную: без этого её отказы сохранялись,
                    # лишь если после неё шла строка, дошедшая до break_match.
                    db.commit()
                continue

            cluster_before = len(match.products)
            print(
                f"  match_id={r.match_id} detach product_id={r.detach_product_id} "
                f"(cluster size {cluster_before}) — reason: {r.reason!r}"
            )
            if not apply:
                continue
            rej_count = match_actions.break_match(
                db,
                match_id=r.match_id,
                detach_product_id=r.detach_product_id,
                reason=r.reason or "bulk_cleanup",
            )
            rejections_written += rej_count
            # Распущен ли кластер — по самому кластеру, а не по списку его
            # участников: `match` после удаления — объект вне сессии, а его
            # `products` — состав до отвязки.
            if db.get(storage.Match, r.match_id) is None:
                matches_dissolved += 1

    if not apply:
        print("\nDry-run. Pass --apply to commit changes.")
    else:
        print(
            f"\nDONE. rejections_written={rejections_written} "
            f"matches_dissolved={matches_dissolved} skipped={skipped}"
        )
    return rejections_written


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv", type=Path, help="Path to CSV with rejection pairs")
    p.add_argument("--apply", action="store_true", help="Actually commit (default: dry-run)")
    args = p.parse_args()

    if not args.csv.exists():
        print(f"ERROR: {args.csv} not found", file=sys.stderr)
        return 1
    try:
        cleanup(args.csv, apply=args.apply)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
