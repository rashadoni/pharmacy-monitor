#!/usr/bin/env python3
"""Stage-2 recall recovery: find cross-site product pairs the bucketer MISSED but
that are genuinely the same product, via a coarse first-token block + the matcher's
full hard-guard arbitration.

Recall root (audit): match_products only scores pairs in the SAME bucket_key
(brand, dosage, pack). True twins diverge on those keys (manufacturer-vs-tradename
brand, stale/variant pack). This generates candidates blocked ONLY on the first
significant name token (much coarser), keeps cross-site pairs with token_set_ratio
>= threshold that pass EVERY hard guard (_hard_conflict + _pairwise_spec_conflict)
and aren't already rejected.

READ-ONLY by default → prints count + sample for eyeball/verification. With --commit
it would link them (is_manual=False) — but run dry first and verify precision.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rapidfuzz import fuzz  # noqa: E402
from sqlalchemy import select  # noqa: E402

from src import matcher, storage  # noqa: E402
from src.storage import MatchRejection, Product  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fuzz", type=int, default=85, help="min token_set_ratio")
    ap.add_argument("--max-block", type=int, default=250, help="skip oversized generic blocks")
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--strict", action="store_true", help="require equal pack/dose/variant/form")
    ap.add_argument(
        "--ultra",
        action="store_true",
        help="strict + equal significant-token SET (not subset) + equal size-letter",
    )
    ap.add_argument("--commit", action="store_true", help="link the candidates (is_manual=False)")
    args = ap.parse_args()
    if args.ultra:
        args.strict = True
    SIZES = {"xs", "s", "m", "l", "xl", "xxl", "xxxl"}

    import re as _re

    def _size_tokens(name: str) -> frozenset[str]:
        return frozenset(t for t in _re.split(r"[^a-zəçşğöüı]+", (name or "").lower()) if t in SIZES)
    db = os.environ.get("DATABASE_URL")
    if not db:
        print("ERROR: set DATABASE_URL", file=sys.stderr)
        return 2
    Session = storage.make_session(db)
    with Session() as s:
        # preload rejections (avoid per-pair DB hit)
        rej = set()
        for a, b in s.execute(select(MatchRejection.product_a_id, MatchRejection.product_b_id)):
            rej.add((a, b))
            rej.add((b, a))
        prods = s.scalars(select(Product).where(Product.url_dead_at.is_(None))).all()
        unmatched = [p for p in prods if p.canonical_id is None and p.name_normalized]
        blocks = defaultdict(list)
        for p in unmatched:
            toks = matcher._significant_name_tokens(p.name_normalized)
            first = next((t for t in p.name_normalized.split() if t in toks), None)
            if first:
                blocks[first].append(p)
        cands = []
        skipped_blocks = 0
        for tok, group in blocks.items():
            if len(group) > args.max_block:
                skipped_blocks += 1
                continue
            for i, a in enumerate(group):
                for b in group[i + 1 :]:
                    if a.site == b.site:
                        continue
                    sc = fuzz.token_set_ratio(a.name_normalized, b.name_normalized)
                    if sc < args.fuzz:
                        continue
                    if (a.id, b.id) in rej:
                        continue
                    if matcher._hard_conflict(a, b) or matcher._pairwise_spec_conflict(a, b):
                        continue
                    if args.strict:
                        from src.normalize import extract_pack_size

                        if extract_pack_size(a.name) != extract_pack_size(b.name):
                            continue
                        if matcher._doses_mg(a.name) != matcher._doses_mg(b.name):
                            continue
                        if matcher._variant_words(a.name) != matcher._variant_words(b.name):
                            continue
                        if matcher.extract_form(a.name) != matcher.extract_form(b.name):
                            continue
                    if args.ultra:
                        if matcher._significant_name_tokens(
                            a.name_normalized
                        ) != matcher._significant_name_tokens(b.name_normalized):
                            continue
                        if _size_tokens(a.name) != _size_tokens(b.name):
                            continue
                        # EQUAL ingredient codes (not omission-tolerant): excludes
                        # "Dekol" vs "Dekol+K2", "Venatura" vs "Venatura K2" — the
                        # D3-vs-D3+K2 composition class the client flagged.
                        if matcher._ingredient_codes(a.name) != matcher._ingredient_codes(b.name):
                            continue
                    cands.append((sc, a, b))
        cands.sort(key=lambda r: r[0], reverse=True)
        print(f"unmatched products: {len(unmatched)} | blocks: {len(blocks)} (skipped {skipped_blocks} > {args.max_block})")
        print(f"RECALL CANDIDATES (fuzz>={args.fuzz}, guards pass, not rejected): {len(cands)}")
        print("--- sample (score | site:name ✗ site:name) — EYEBALL for false matches ---")
        # sample across the score range
        step = max(1, len(cands) // args.sample)
        for sc, a, b in cands[:: step][: args.sample]:
            print(f"  {sc:.0f}  [{a.site[:3]}] {(a.name or '').strip()[:32]}  ✗  [{b.site[:3]}] {(b.name or '').strip()[:32]}")
        if args.commit:
            linked = 0
            used: set[int] = set()
            for _sc, a, b in cands:
                # greedy: skip if either product already linked this run or now has a cluster
                if a.id in used or b.id in used:
                    continue
                if a.canonical_id is not None or b.canonical_id is not None:
                    continue
                matcher._persist_match(s, [a, b], confidence=0.9)
                used.add(a.id)
                used.add(b.id)
                linked += 1
            s.commit()
            print(f"\nCOMMITTED: linked {linked} new cross-site pairs (is_manual=False)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
