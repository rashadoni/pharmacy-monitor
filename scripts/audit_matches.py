#!/usr/bin/env python3
"""Proactive mismatch audit: rank cross-site clusters by 'looks like a mismatch'.

The signal the client used to spot every bad match by eye: prices differ a lot AND
the names differ in a meaningful way. This scans all auto cross-site clusters and
ranks those with a price spread >= --ratio AND a name/spec divergence the existing
guards don't already catch — surfacing the NEXT classes (extra ingredient WORDS like
"forte"/"plus"/a flavor, dosage/strength differences, leftover brand/code diffs).

Clusters with a big price spread but IDENTICAL names are NOT mismatches — they're
genuine price gaps (the product's whole point) — so they're excluded from the suspect
list (shown separately under --show-gaps).

Read-only. Run from Mac against prod via the SSH tunnel (DATABASE_URL).
"""

from __future__ import annotations

import argparse
import itertools
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from src import brand_resolver, matcher, storage  # noqa: E402
from src.normalize import strip_accents  # noqa: E402
from src.storage import Match, latest_snapshots_per_product  # noqa: E402

_UNITS = {
    "ml",
    "mg",
    "mq",
    "qr",
    "gr",
    "kg",
    "mkg",
    "mcg",
    "ed",
    "iu",
    "sm",
    "cm",
    "damci",
    "damla",
    "tablet",
    "tableti",
    "kapsul",
    "kapsulalar",
    "kapsula",
    "kapsullar",
    "ampul",
    "ampula",
    "sherbet",
    "sirop",
    "siropu",
    "drops",
    "eded",
    "adet",
    "flakon",
    "tüp",
    "tup",
    "saše",
    "sase",
    "vial",
    "spr",
}


def _sig_words(name: str) -> set[str]:
    """Significant word-tokens of a name: len>=3, no digit, not a unit/form/noise.
    Captures distinguishing WORDS (forte, plus, baby, a flavor, an extra ingredient
    word) — vitamin codes like d3/k2 are handled separately by the ingredient guard."""
    toks = re.split(r"[^a-z0-9əçşğöüı]+", strip_accents(name or "").lower())
    out = set()
    for t in toks:
        if len(t) < 3 or any(ch.isdigit() for ch in t):
            continue
        if t in _UNITS or t in matcher._MATCH_NOISE_TOKENS:
            continue
        out.add(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ratio", type=float, default=1.8, help="min cross-site price spread")
    ap.add_argument("--top", type=int, default=50, help="how many suspects to print")
    ap.add_argument(
        "--show-gaps", action="store_true", help="also list big price gaps w/ identical names"
    )
    args = ap.parse_args()
    db = os.environ.get("DATABASE_URL")
    if not db:
        print("ERROR: set DATABASE_URL", file=sys.stderr)
        return 2
    Session = storage.make_session(db)
    with Session() as s:
        matches = s.scalars(
            select(Match).where(Match.is_manual.is_(False)).options(selectinload(Match.products))
        ).all()
        all_ids = [p.id for m in matches for p in m.products]
        snaps = latest_snapshots_per_product(s, all_ids)

        def price(p):
            sp = snaps.get(p.id)
            if not sp:
                return None
            return sp.discount_price if sp.discount_price is not None else sp.price

        suspects, gaps, scanned = [], [], 0
        for m in matches:
            prods = [p for p in m.products if price(p)]
            sites = {p.site for p in prods}
            if len(sites) < 2:
                continue
            scanned += 1
            # the widest cross-site price pair
            best = None
            for a, b in itertools.combinations(prods, 2):
                if a.site == b.site:
                    continue
                pa, pb = price(a), price(b)
                ratio = max(pa, pb) / min(pa, pb)
                if best is None or ratio > best[0]:
                    best = (ratio, a, b)
            if not best:
                continue
            ratio, a, b = best
            if ratio < args.ratio:
                continue
            reasons = []
            if matcher._has_conflicting_ingredient_codes(a, b):
                reasons.append("ingredient")
            if matcher._has_conflicting_strength_number(a.name or "", b.name or ""):
                reasons.append("strength")
            if matcher._has_conflicting_variant_marker(a.name or "", b.name or ""):
                reasons.append("variant")
            if brand_resolver.brands_conflict(a.brand_verified, b.brand_verified):
                reasons.append("brand")
            extra = _sig_words(a.name) ^ _sig_words(b.name)
            if extra:
                reasons.append("words:" + ",".join(sorted(extra)[:4]))
            row = (ratio, m.id, reasons, a, b, price(a), price(b))
            (suspects if reasons else gaps).append(row)

        suspects.sort(key=lambda r: r[0], reverse=True)
        print(f"scanned {scanned} cross-site clusters (both sides priced)")
        print(f"SUSPECTS (spread >= {args.ratio}x AND name/spec divergence): {len(suspects)}\n")
        for ratio, cid, reasons, a, b, pa, pb in suspects[: args.top]:
            print(f"cl{cid}  {ratio:.1f}x  [{'; '.join(reasons)}]")
            print(f"     [{a.site:<11}] {pa:>7.2f}  {(a.name or '').strip()[:40]}")
            print(f"     [{b.site:<11}] {pb:>7.2f}  {(b.name or '').strip()[:40]}")
        if args.show_gaps:
            gaps.sort(key=lambda r: r[0], reverse=True)
            print(f"\n--- big price gaps, IDENTICAL names (genuine, not mismatch): {len(gaps)} ---")
            for ratio, cid, _r, a, b, pa, pb in gaps[:15]:
                print(f"  cl{cid} {ratio:.1f}x  {pa:.2f}/{pb:.2f}  «{(a.name or '').strip()[:34]}»")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
