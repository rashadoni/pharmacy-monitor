#!/usr/bin/env python3
"""Classify cross-site AUTO commodity clusters by available conflict signal
(brand / country / asymmetric-grade) — to decide what can be auto-cleaned safely
vs needs human/LLM review. Prints counts + samples.

Read-only BY DEFAULT. `--apply` MUTATES: it writes MatchRejection rows, nulls
`canonical_id` and DELETES the Match.

⚠️ `--apply` IS NOT REVERSIBLE BY TOOLING. `rollback_match_policy.py` finds work
only through `MatchPolicyAudit` rows, which are emitted solely by
`matcher.revalidate_split` (src/matcher.py:1446) — this script emits none, and
`--apply` destroys the before-image (the Match row) that a rollback would need.
`reason_type` alone does not make a rejection revertible. Take a DB snapshot
first, or prefer `revalidate_split`, which does the same job with a coherent
split instead of dissolve-all, and is audited and rollback-able.
(The old docstring claimed "READ-ONLY … No mutation" while `--apply` deleted
clusters; it was wrong from the commit that added `--apply`.)"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from src import matcher, storage  # noqa: E402
from src.brand_resolver import consumer_brand, is_commodity_name  # noqa: E402
from src.normalize import strip_accents  # noqa: E402

_GRADE = {"kosmetik", "kosmetika", "kosmeticeskoe", "naruzhnoe", "cosmetic"}


def _grade_tokens(name: str) -> frozenset[str]:
    # strip_accents to stay identical to matcher._grade_tokens. Without it the
    # Azerbaijani dotted `İ` lowercases to `i` + combining dot, so `KOSMETİK YAĞ`
    # reads as no-grade here while the matcher sees a grade — this audit would
    # under-report against the very guard it audits.
    t = strip_accents((name or "").lower())
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
    Conservative: same-brand (Medoil tr↔az), same-grade, or same-country are kept.

    NB: uses `_has_conflicting_legacy_country`, NOT `_has_conflicting_country`.
    The latter was narrowed to verified `country_code_of` only, so a row whose
    country is known solely from `manufacturer` or the URL slug reads as "no
    conflict" and this audit would report falsely clean on the very policy it
    exists to enforce. This audit is already commodity-gated, which is the guard
    that made the legacy signal safe in the first place."""
    ms = list(m.products)
    if len({p.site for p in ms}) < 2 or not all(is_commodity_name(p.name) for p in ms):
        return False
    pairs = [(a, b) for i, a in enumerate(ms) for b in ms[i + 1 :] if a.site != b.site]
    if _same_recoverable_brand(pairs):
        return False
    return any(
        _grade_tokens(a.name) != _grade_tokens(b.name)
        and matcher._has_conflicting_legacy_country(a, b)
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
    # client policy 2026-05-31: country must match too — NO same-brand exception
    # (Medoil Türkiyə ≠ Medoil Azərbaycan).
    return any(
        matcher._has_conflicting_legacy_country(a, b)
        or _grade_tokens(a.name) != _grade_tokens(b.name)
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
            # The samples below are labelled "NOT targeted", so a cluster that IS a
            # target must never appear in them. Under --strict every country-conflict
            # cluster is a target, and the sample printed all 46 of them under a
            # "NOT targeted" heading while --apply dissolved them.
            is_target = target_fn(m)
            if is_target:
                targets.append(m)
            if any(matcher._has_conflicting_brand(a, b) for a, b in pairs):
                brand_conf += 1
                continue
            # Same predicate as the target rules above. If this stays on the
            # verified-only `_has_conflicting_country`, a cluster with a
            # legacy-only country conflict is printed as "no signal (NOT
            # targeted)" while `--apply` dissolves it in the same run — the
            # readout would contradict the destructive action it precedes.
            if any(matcher._has_conflicting_legacy_country(a, b) for a, b in pairs):
                country_conf += 1
                if not is_target and len(s_country) < args.sample:
                    s_country.append(m)
                continue
            if any(_grade_tokens(a.name) != _grade_tokens(b.name) for a, b in pairs):
                grade_asym += 1
                if not is_target and len(s_grade) < args.sample:
                    s_grade.append(m)
                continue
            neither += 1
            if not is_target and len(s_neither) < args.sample:
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
                            # Label by the evidence that actually fired, mirroring
                            # matcher.revalidate_split. `_strict_not_identical` also
                            # targets grade-only clusters, so hardcoding "country"
                            # would stamp a grade split as a country one.
                            country_conflict = matcher._has_conflicting_legacy_country(a, b)
                            add_rejection(
                                s,
                                a.id,
                                b.id,
                                reason=(
                                    "cross-origin" if country_conflict else "grade mismatch"
                                ),
                                reason_type=(
                                    "system_country" if country_conflict else "system_spec"
                                ),
                            )
                for p in ms:
                    p.canonical_id = None
                s.delete(m)
                n += 1
            s.commit()
            print(f"\nAPPLIED: dissolved {n} clusters (+ rejections) → members to /matcher queue")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
