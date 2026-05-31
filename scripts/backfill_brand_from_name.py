#!/usr/bin/env python3
"""Recover brand_verified from the product NAME via the known-brand vocabulary.

Commodity oils carry the brand inside the NAME, not the slug: pharmonline
"Naftalan yağı 60 ml (Medoil)", aptekonline 'Çaytikanı yağı "Səidə" 100 ml'.
backfill_aptek_slug_brand.py only reads the slug (which is brandless, e.g.
OBLEPIXA--MASLO--100ml), so these stayed NULL and the matcher's brand guard
could not act — letting cross-brand oils (Medoil vs Biola) cluster.

This matches match_brand_in_text against the NAME for every commodity product
with brand_verified NULL (all three sites), using the same >=min-freq consumer-
brand vocab (only known brands can match → no junk). Populating both sides lets
revalidate_split SPLIT genuine cross-brand pairs while KEEPING same-brand twins
(Medoil↔Medoil), instead of a blunt dissolve that would kill the correct ones.

Additive/reversible. Dry-run by default; --commit applies. Run on prod.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from src import brand_resolver, storage  # noqa: E402


def _build_vocab(session, min_freq: int) -> set[str]:
    rows = session.scalars(
        select(storage.Product.brand_verified).where(storage.Product.brand_verified.is_not(None))
    ).all()
    freq: Counter[str] = Counter()
    for bv in rows:
        cb = brand_resolver.consumer_brand(bv)
        if cb and len(cb) >= 4:
            freq[cb] += 1
    return {b for b, n in freq.items() if n >= min_freq}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-freq", type=int, default=3, help="min vocab brand frequency")
    ap.add_argument("--commit", action="store_true", help="apply (default: plan only)")
    ap.add_argument("--sample", type=int, default=30)
    args = ap.parse_args()
    db = os.environ.get("DATABASE_URL")
    if not db:
        print("ERROR: set DATABASE_URL", file=sys.stderr)
        return 2
    Session = storage.make_session(db)
    with Session() as s:
        vocab = _build_vocab(s, args.min_freq)
        print(f"vocab (consumer brands, freq>={args.min_freq}): {len(vocab)}")
        cand = s.scalars(
            select(storage.Product).where(
                storage.Product.brand_verified.is_(None),
                storage.Product.canonical_id.is_not(None),  # in a cluster = actionable
            )
        ).all()
        cand = [p for p in cand if brand_resolver.is_commodity_name(p.name)]
        filled = 0
        samples = []
        for p in cand:
            brand = brand_resolver.match_brand_in_text(p.name, vocab)
            if brand:
                filled += 1
                if len(samples) < args.sample:
                    samples.append((p.site, brand, (p.name or "").strip()[:42]))
                if args.commit:
                    p.brand_verified = brand.title()
        if args.commit:
            s.commit()
        verb = "COMMITTED" if args.commit else "plan only"
        print(f"commodity products in clusters, brand NULL: {len(cand)}")
        print(f"brand recovered from NAME: {filled}  ({verb})")
        print("--- sample (site | brand ← name) ---")
        for site, brand, name in samples:
            print(f"  {site[:3]:<3} | {brand:<14} ← {name}")
        if not args.commit:
            print("\n(plan only — pass --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
