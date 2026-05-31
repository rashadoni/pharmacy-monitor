#!/usr/bin/env python3
"""Brand-coherent re-cluster: split clusters flagged by the brand-conflict guard.

The CLI `rematch --revalidate` dissolves a flagged cluster wholesale and rejects
ALL member pairs — wrong for a 3+ member cluster like cl101216 (aloe Xerbes +
aptek Herba Flora + pharmonline Herba Flora), where the two Herba Flora products
are a CORRECT same-brand match that must be preserved.

This splits each flagged cluster into brand-coherent groups (members that do NOT
`brands_conflict` form a group), keeps the largest cross-site group as the Match,
and ejects the outliers (canonical_id=None + a rejection against each kept member,
so the bad pair never re-matches). If no group has a cross-site pair, the whole
cluster is dissolved.

Idempotent-ish and bounded (only touches guard-flagged auto clusters). Pass
--commit to apply; default prints the plan only.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import match_actions, matcher, storage  # noqa: E402


def _brand_groups(members: list) -> list[list]:
    """Connected components where an edge = two members that do NOT spec-conflict.
    Uses the full _pairwise_spec_conflict (brand + ingredient-code + variant/strength
    /dimension/concentration), so it coherently splits ANY conflicting cluster — e.g.
    keeps a same-brand Herba Flora pair while ejecting a Xerbes outlier, and separates
    D3 from D3+K2. Members with no conflicting signal attach to the first group."""
    groups: list[list] = []
    for p in members:
        placed = False
        for g in groups:
            if all(not matcher._pairwise_spec_conflict(p, q) for q in g):
                g.append(p)
                placed = True
                break
        if not placed:
            groups.append([p])
    return groups


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="apply (default: plan only)")
    args = ap.parse_args()
    db = os.environ.get("DATABASE_URL")
    if not db:
        print("ERROR: set DATABASE_URL", file=sys.stderr)
        return 2
    Session = storage.make_session(db)
    with Session() as s:
        flagged = matcher.find_conflicting_clusters(s)
        # dedupe to unique matches (find_conflicting_clusters returns one pair per match)
        seen: set[int] = set()
        actions = 0
        for m, _a, _b in flagged:
            if m.id in seen:
                continue
            seen.add(m.id)
            members = list(m.products)
            groups = sorted(_brand_groups(members), key=len, reverse=True)
            # the kept group: largest with a cross-site pair
            keep = next((g for g in groups if len({p.site for p in g}) >= 2), None)
            eject = [p for p in members if keep is None or p not in keep]
            label = (m.canonical_name or "")[:34]
            if keep is None:
                print(
                    f"cl{m.id} «{label}»: DISSOLVE ({len(members)} members, no coherent cross-site group)"
                )
                for i, x in enumerate(members):
                    for y in members[i + 1 :]:
                        if args.commit:
                            match_actions.add_rejection(s, x.id, y.id, reason="spec-conflict")
                for p in members:
                    if args.commit:
                        p.canonical_id = None
                if args.commit:
                    s.delete(m)
                actions += 1
            else:
                print(
                    f"cl{m.id} «{label}»: KEEP {[p.id for p in keep]}, "
                    f"EJECT {[(p.id, p.brand_verified) for p in eject]}"
                )
                for p in eject:
                    for q in keep:
                        if args.commit:
                            match_actions.add_rejection(s, p.id, q.id, reason="spec-conflict")
                    if args.commit:
                        p.canonical_id = None
                actions += 1
        if args.commit:
            s.commit()
            print(f"\nCOMMITTED: {actions} clusters re-split")
        else:
            print(f"\n(plan only — {actions} clusters; pass --commit to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
