"""Phase 2.4 — bulk re-match of low-confidence matches after barcode fill.

После того как scrape пробежал и часть продуктов получила `barcode`, существующие
fuzzy-matched clusters (confidence < 0.85) могут оказаться:
  1. **Подтверждены барkodом** — barcode совпадает между сайтами → cluster valid
  2. **Опровергнуты барkodом** — разные barcode → cluster нужно разорвать
  3. **Без сигнала** — barcode null или у одного продукта → оставляем как fuzzy

Этот скрипт:
  - НЕ трогает `is_manual=True` (человек подтвердил/закрепил)
  - НЕ трогает clusters с confidence ≥ 0.85 (уверенный fuzzy match)
  - Для остальных: если barcode противоречит → break_match
  - В конце: вызывает matcher.match_products → barcode pass подберёт новые pairs

Idempotent. Запуск повторно безопасен.

Usage:
    # Dry-run (рекомендуется):
    DATABASE_URL=postgresql://... python -m scripts.rematch_with_barcode

    # Apply:
    DATABASE_URL=postgresql://... python -m scripts.rematch_with_barcode --apply

    # Custom threshold:
    DATABASE_URL=postgresql://... python -m scripts.rematch_with_barcode \
        --confidence-cutoff 0.90 --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src import match_actions, matcher, storage  # noqa: E402


def _cluster_barcodes_disagree(products: list[storage.Product]) -> bool:
    """True if cluster has ≥2 products with non-null barcode AND they differ."""
    barcodes = [
        (p.barcode or "").strip()
        for p in products
        if p.barcode and p.barcode.strip() and p.barcode.strip().isdigit()
    ]
    distinct = {bc for bc in barcodes if len(bc) >= 8}
    return len(distinct) >= 2


def rematch(apply: bool, confidence_cutoff: float) -> int:
    """Return number of clusters broken. 0 if dry-run unless --apply."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        return -1

    Session = storage.make_session(db_url)
    broken_clusters = 0

    with Session() as db:
        # Все clusters с confidence ниже порога, не is_manual
        all_matches = db.scalars(
            select(storage.Match).where(
                storage.Match.confidence < confidence_cutoff,
                storage.Match.is_manual.is_(False),
            )
        ).all()
        print(
            f"Loaded {len(all_matches)} clusters with confidence < "
            f"{confidence_cutoff} (excluding is_manual)."
        )

        for m in all_matches:
            products = list(m.products)
            if len(products) < 2:
                continue
            if not _cluster_barcodes_disagree(products):
                continue

            barcodes_pretty = sorted({(p.site, p.barcode) for p in products if p.barcode})
            print(
                f"  match_id={m.id} conf={m.confidence:.2f} "
                f"name={m.canonical_name!r} barcodes={barcodes_pretty}"
            )

            if not apply:
                broken_clusters += 1
                continue

            # Identify the product whose barcode is the OUTLIER. If we can't
            # tell, break all pairings via add_rejection between all pairs and
            # null canonical_id on all members.
            #
            # Simpler conservative approach: break the whole cluster (all
            # canonical_id → None) + add_rejection for every cross-site pair.
            for p in products:
                p.canonical_id = None
            for i, p in enumerate(products):
                for q in products[i + 1 :]:
                    if p.site == q.site:
                        continue
                    match_actions.add_rejection(
                        db,
                        p.id,
                        q.id,
                        reason=f"rematch_v2: barcode disagree (pre={m.confidence:.2f})",
                    )
            db.delete(m)
            broken_clusters += 1

        if apply:
            db.commit()
            print(f"\nBroke {broken_clusters} cluster(s). Now re-running matcher...")
            # Re-run matcher to let barcode pass pick up corrected matches.
            new_or_updated = matcher.match_products(db)
            db.commit()
            print(f"Matcher re-run: {new_or_updated} new/updated.")
        else:
            print(f"\nDry-run. Would break {broken_clusters} cluster(s). Pass --apply.")

    return broken_clusters


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Commit changes (default: dry-run)")
    p.add_argument(
        "--confidence-cutoff",
        type=float,
        default=0.85,
        help="Only consider matches with confidence < this (default 0.85)",
    )
    args = p.parse_args()
    rematch(apply=args.apply, confidence_cutoff=args.confidence_cutoff)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
