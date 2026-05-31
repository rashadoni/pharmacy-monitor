#!/usr/bin/env python3
"""READ-ONLY: classify cross-site AUTO commodity clusters by available conflict
signal (brand / country / asymmetric-grade) — to decide what can be auto-cleaned
safely vs needs human/LLM review. Prints counts + samples. No mutation."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from src import matcher, storage  # noqa: E402
from src.brand_resolver import consumer_brand, is_commodity_name  # noqa: E402

_GRADE = {"kosmetik", "kosmetika", "kosmeticeskoe", "naruzhnoe", "cosmetic"}


def _grade_tokens(name: str) -> frozenset[str]:
    t = (name or "").lower()
    return frozenset(g for g in _GRADE if g in t)


def _same_recoverable_brand(pairs) -> bool:
    """Any cross-site pair shares the same resolvable consumer brand (Medoil↔Medoil)
    → the cluster is a confirmed same-brand commodity, never split it."""
    for a, b in pairs:
        ca, cb = consumer_brand(a.brand_verified), consumer_brand(b.brand_verified)
        if ca and cb and ca == cb:
            return True
    return False


def _cosmetic_origin_false(m) -> bool:
    """The client's class: COMMODITY cluster where some cross-site pair differs in
    GRADE (one cosmetic/kosmetik, the other not) AND in COUNTRY of origin, and NO
    pair shares a consumer brand. Cosmetic ru-oil vs food az-oil = different product.
    Conservative: same-brand (Medoil tr↔az), same-grade, or same-country are kept."""
    ms = list(m.products)
    if len({p.site for p in ms}) < 2 or not all(is_commodity_name(p.name) for p in ms):
        return False
    pairs = [(a, b) for i, a in enumerate(ms) for b in ms[i + 1 :] if a.site != b.site]
    if _same_recoverable_brand(pairs):
        return False
    return any(
        _grade_tokens(a.name) != _grade_tokens(b.name) and matcher._has_conflicting_country(a, b)
        for a, b in pairs
    )


def _strict_not_identical(m) -> bool:
    """STRICT (client: «только идентичные товары и ТОЛЬКО»): a COMMODITY cluster is
    NOT identical if some cross-site pair differs in COUNTRY of origin OR in GRADE
    (cosmetic/kosmetik), UNLESS a pair confirms the SAME consumer brand. Same-brand
    (Medoil tr↔az) and no-distinguishing-difference (same country+grade, e.g.
    Gənəgərçək az↔az) are kept as identical; everything else → split to /matcher."""
    ms = list(m.products)
    if len({p.site for p in ms}) < 2 or not all(is_commodity_name(p.name) for p in ms):
        return False
    pairs = [(a, b) for i, a in enumerate(ms) for b in ms[i + 1 :] if a.site != b.site]
    if _same_recoverable_brand(pairs):
        return False
    return any(
        matcher._has_conflicting_country(a, b) or _grade_tokens(a.name) != _grade_tokens(b.name)
        for a, b in pairs
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=15)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="dissolve the targeted clusters (+ rejection)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="STRICT: split ALL non-identical commodity (diff country OR grade, no shared brand)",
    )
    args = ap.parse_args()
    target_fn = _strict_not_identical if args.strict else _cosmetic_origin_false
    Session = storage.make_session(os.environ["DATABASE_URL"])
    with Session() as s:
        matches = s.scalars(select(storage.Match).where(storage.Match.is_manual.is_(False))).all()
        comm = brand_conf = country_conf = grade_asym = neither = 0
        targets = []
        s_country, s_grade, s_neither = [], [], []
        for m in matches:
            ms = list(m.products)
            if len({p.site for p in ms}) < 2:
                continue
            if not all(is_commodity_name(p.name) for p in ms):
                continue
            comm += 1
            pairs = [(a, b) for i, a in enumerate(ms) for b in ms[i + 1 :] if a.site != b.site]
            if target_fn(m):
                targets.append(m)
            if any(matcher._has_conflicting_brand(a, b) for a, b in pairs):
                brand_conf += 1
                continue
            if any(matcher._has_conflicting_country(a, b) for a, b in pairs):
                country_conf += 1
                if len(s_country) < args.sample:
                    s_country.append(m)
                continue
            if any(_grade_tokens(a.name) != _grade_tokens(b.name) for a, b in pairs):
                grade_asym += 1
                if len(s_grade) < args.sample:
                    s_grade.append(m)
                continue
            neither += 1
            if len(s_neither) < args.sample:
                s_neither.append(m)

        def show(title, lst):
            print(f"\n--- {title} (sample) ---")
            for m in lst:
                parts = []
                for p in m.products:
                    parts.append(
                        f"{p.site[:3]}:{(p.name or '')[:30]}"
                        f"[{matcher._country_of(p) or '?'}/{consumer_brand(p.brand_verified) or '-'}]"
                    )
                print(f"  cl{m.id}: " + " | ".join(parts))

        print(f"cross-site COMMODITY auto clusters: {comm}")
        print(f"  brand-conflict (already auto-split by revalidate): {brand_conf}")
        print(f"  country-conflict only: {country_conf}")
        print(f"  asymmetric-grade only: {grade_asym}")
        print(f"  no signal (brand/country/grade all silent): {neither}")
        print(f"\n>>> PRECISE TARGET (cosmetic-grade + diff-origin + no shared brand): {len(targets)}")
        show("TARGET to dissolve", targets[: args.sample])
        show("COUNTRY-conflict (NOT targeted — generic/same-brand)", s_country)
        show("NO-signal (NOT targeted)", s_neither)

        if args.apply:
            from src.match_actions import add_rejection

            n = 0
            for m in targets:
                ms = list(m.products)
                for i, a in enumerate(ms):
                    for b in ms[i + 1 :]:
                        if a.site != b.site:
                            add_rejection(s, a.id, b.id, reason="cosmetic vs food cross-origin")
                for p in ms:
                    p.canonical_id = None
                s.delete(m)
                n += 1
            s.commit()
            print(f"\nAPPLIED: dissolved {n} clusters (+ rejections) → members to /matcher queue")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
