"""Auto-confirm matches where all products with barcode agree (Phase 2.5 cleanup).

After Phase 2.3 barcode-matcher landed, many low-confidence fuzzy matches actually
have barcode agreement — meaning the fuzzy match was right but the matcher's
confidence score doesn't reflect that. This script promotes those clusters to
`is_manual=True` without human review.

Rules (all must be true to auto-confirm):
  - Match is NOT already manual (is_manual=false)
  - At least 2 products in cluster have non-null, valid barcode (8-14 digits)
  - All those barcoded products share the SAME barcode
  - No MatchRejection records exist for any pair in this cluster

Conservative: products without barcode in the cluster don't block confirmation
(barcode agreement on 2 of 3 sites is strong enough signal).

Idempotent — running twice is safe.

Usage:
    DATABASE_URL=postgresql://... python -m scripts.auto_confirm_barcode_matches
    DATABASE_URL=... python -m scripts.auto_confirm_barcode_matches --apply
    DATABASE_URL=... python -m scripts.auto_confirm_barcode_matches --apply --max 500
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src import match_actions, storage  # noqa: E402


def _valid_barcode(s: str | None) -> bool:
    if not s:
        return False
    bc = s.strip()
    return bc.isdigit() and 8 <= len(bc) <= 14 and bc not in ("0", "00000000")


def _cluster_barcode_unanimous(products: list[storage.Product]) -> str | None:
    """Return the shared barcode if ≥2 products agree, else None."""
    barcodes = [p.barcode.strip() for p in products if p.barcode and _valid_barcode(p.barcode)]
    if len(barcodes) < 2:
        return None
    distinct = set(barcodes)
    return barcodes[0] if len(distinct) == 1 else None


def _has_any_rejection(db, products: list[storage.Product]) -> bool:
    """True if any pair in this cluster has been manually rejected."""
    for i, a in enumerate(products):
        for b in products[i + 1 :]:
            if match_actions.is_rejected(db, a.id, b.id):
                return True
    return False


def auto_confirm(apply: bool = False, max_count: int | None = None) -> dict:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return {"error": "no_db"}

    Session = storage.make_session(db_url)
    stats = {"scanned": 0, "candidates": 0, "confirmed": 0, "rejected_skip": 0}

    with Session() as db:
        # Look at all auto matches (not yet manual). Don't filter by confidence —
        # even high-conf matches benefit from is_manual=True (locks them in
        # against future auto-matcher mutations).
        matches = db.scalars(select(storage.Match).where(storage.Match.is_manual.is_(False))).all()
        stats["scanned"] = len(matches)

        for m in matches:
            if max_count is not None and stats["confirmed"] >= max_count:
                break
            products = list(m.products)
            if len(products) < 2:
                continue
            shared_bc = _cluster_barcode_unanimous(products)
            if not shared_bc:
                continue
            stats["candidates"] += 1
            if _has_any_rejection(db, products):
                stats["rejected_skip"] += 1
                continue
            if apply:
                m.is_manual = True
                if m.needs_review:
                    m.needs_review = False
            stats["confirmed"] += 1
            print(
                f"  match_id={m.id} bc={shared_bc} sites={','.join(p.site for p in products)} "
                f"name={m.canonical_name[:50]!r}"
            )

        if apply:
            db.commit()
            print(f"\nCommitted {stats['confirmed']} confirmations.")
        else:
            print(f"\nDRY-RUN. Would confirm {stats['confirmed']} matches. Pass --apply.")

    print(f"Stats: {stats}")
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Commit changes (default: dry-run)")
    p.add_argument("--max", type=int, default=None, help="Cap confirmations at N")
    args = p.parse_args()
    auto_confirm(apply=args.apply, max_count=args.max)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
