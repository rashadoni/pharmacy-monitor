"""Загрузить categories в БД из data/category_map.json.

Стратегия pilot: создать row для каждой найденной категории на каждом сайте,
без cross-site matching (это можно сделать позже вручную для конкретных групп).

Для каждой row:
  - slug_pharmonline / slug_aptekonline_id / slug_aloe — заполнен только тот сайт
    откуда категория, остальные NULL.
  - is_active=True
  - name_az = name из discovery
"""

from __future__ import annotations

import json
from pathlib import Path
from src.storage import init_db, make_session, Category
from sqlalchemy import select

MAP_FILE = Path("data/category_map.json")


def main() -> None:
    init_db()
    S = make_session()
    session = S()

    cats = json.loads(MAP_FILE.read_text())
    print(f"Loaded {len(cats)} categories from map\n")

    # Filter aptekonline: только числовые ID (это category ID)
    filtered = []
    for c in cats:
        if c["site"] == "aptekonline":
            if not c["slug"].isdigit():
                continue
        if c["site"] == "aloe":
            # Удалить trailing slash
            c["slug"] = c["slug"].rstrip("/")
        filtered.append(c)

    print(f"After filter: {len(filtered)}\n")

    # Существующие slug'и в БД (чтобы не дублировать)
    existing_pharma = {
        s
        for (s,) in session.execute(
            select(Category.pharmonline_slug).where(Category.pharmonline_slug.isnot(None))
        )
    }
    existing_aptek = {
        s
        for (s,) in session.execute(
            select(Category.aptekonline_slug).where(Category.aptekonline_slug.isnot(None))
        )
    }
    existing_aloe = {
        s
        for (s,) in session.execute(
            select(Category.aloe_slug).where(Category.aloe_slug.isnot(None))
        )
    }
    existing_keys = {s for (s,) in session.execute(select(Category.key))}

    def make_unique_key(prefix, slug):
        base = f"{prefix}_{slug}"[:90]
        if base not in existing_keys:
            existing_keys.add(base)
            return base
        i = 2
        while True:
            cand = f"{base}_{i}"[:90]
            if cand not in existing_keys:
                existing_keys.add(cand)
                return cand
            i += 1

    counts = {"pharmonline": 0, "aptekonline": 0, "aloe": 0, "skipped": 0}
    for c in filtered:
        site = c["site"]
        slug = c["slug"]
        name = c["name"][:200] or slug

        if site == "pharmonline":
            if slug in existing_pharma:
                counts["skipped"] += 1
                continue
            cat = Category(
                key=make_unique_key("pharma", slug),
                label_ru=name,
                label_az=name,
                pharmonline_slug=slug,
                is_active=True,
            )
            session.add(cat)
            counts["pharmonline"] += 1
        elif site == "aptekonline":
            if slug in existing_aptek:
                counts["skipped"] += 1
                continue
            cat = Category(
                key=make_unique_key("aptek", slug),
                label_ru=name,
                label_az=name,
                aptekonline_slug=slug,
                is_active=True,
            )
            session.add(cat)
            counts["aptekonline"] += 1
        elif site == "aloe":
            if slug in existing_aloe:
                counts["skipped"] += 1
                continue
            cat = Category(
                key=make_unique_key("aloe", slug),
                label_ru=name,
                label_az=name,
                aloe_slug=slug,
                is_active=True,
            )
            session.add(cat)
            counts["aloe"] += 1

    session.commit()
    print("Created (per-site):")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    total = session.scalar(select(__import__("sqlalchemy").func.count(Category.id)))
    print(f"\nTotal categories in DB: {total}")
    session.close()


if __name__ == "__main__":
    main()
