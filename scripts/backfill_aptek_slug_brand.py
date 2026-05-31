#!/usr/bin/env python3
"""Recover aptekonline brand_verified from the URL slug via a known-brand vocabulary.

Closes the gap left by scripts/backfill_brand_verified.py: aptek slug-form products
(e.g. .../FEMINIKA--CAY--N25-Herba-Flora-AZE) lack the `"brand":{}` page JSON, and
their slugs are too noisy to parse naively. Instead we match slug/name tokens against
a VOCABULARY of consumer brands already verified elsewhere (aptek page-JSON + aloe +
pharmonline), keeping only brands seen >= --min-freq times so rare junk (a stray
"kosmetik"/"ternofarm" leaked into brand_verified) can't match. Only known brands can
match → no false brands.

Default scope: commodity products in cross-site clusters missing brand_verified (the
actionable set for the matcher's brand guard). Pass --commit to write; dry-run prints.

Run from Mac against prod via the SSH tunnel (DATABASE_URL). Additive/reversible.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

from sqlalchemy import distinct, func, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import brand_resolver, storage  # noqa: E402


def _build_vocab(session, min_freq: int) -> set[str]:
    """Normalized consumer brands seen >= min_freq times in brand_verified."""
    rows = session.scalars(
        select(storage.Product.brand_verified).where(storage.Product.brand_verified.is_not(None))
    ).all()
    freq: Counter[str] = Counter()
    for bv in rows:
        cb = brand_resolver.consumer_brand(bv)
        if cb and len(cb) >= 4:
            freq[cb] += 1
    return {v for v, n in freq.items() if n >= min_freq}


def _cross_site_member():
    return storage.Product.canonical_id.in_(
        select(storage.Product.canonical_id)
        .where(storage.Product.canonical_id.is_not(None))
        .group_by(storage.Product.canonical_id)
        .having(func.count(distinct(storage.Product.site)) >= 2)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-freq", type=int, default=3, help="min vocab brand frequency")
    ap.add_argument("--all-names", action="store_true", help="not just commodity names")
    ap.add_argument("--commit", action="store_true", help="apply (default: plan only)")
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
                storage.Product.site == "aptekonline",
                _cross_site_member(),
                storage.Product.brand_verified.is_(None),
                storage.Product.url_dead_at.is_(None),
            )
        ).all()
        if not args.all_names:
            cand = [p for p in cand if brand_resolver.is_commodity_name(p.name)]
        filled = 0
        for p in cand:
            slug = p.url.rstrip("/").split("/")[-1]
            brand = brand_resolver.match_brand_in_text(
                slug, vocab
            ) or brand_resolver.match_brand_in_text(p.name, vocab)
            if not brand:
                continue
            filled += 1
            print(f"  {brand.title():<16} ← id={p.id} {slug[-40:]}")
            if args.commit:
                p.brand_verified = brand.title()
        if args.commit:
            s.commit()
            print(f"\nCOMMITTED: brand_verified set for {filled}/{len(cand)} aptek products")
        else:
            print(f"\n(plan only — {filled}/{len(cand)} would be set; pass --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
