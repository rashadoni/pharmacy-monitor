#!/usr/bin/env python3
"""Coherent re-split of guard-flagged clusters — thin CLI over matcher.revalidate_split.

Splits each cluster with a cross-site _pairwise_spec_conflict (brand / ingredient-code
/ variant / strength / dimension / concentration) into spec-coherent groups: keeps the
largest cross-site group as the Match, ejects outliers (+rejection); dissolves if no
coherent cross-site group remains. Smarter than `rematch --revalidate`'s old dissolve-all
(preserves a correct same-brand pair while ejecting an outlier).

This is the SAME logic now run automatically after match_products in the scrape pipeline
(src/main.py) — this script is for manual/ad-hoc runs and review. Pass --commit to apply;
default prints the plan only. Run from Mac against prod via the SSH tunnel (DATABASE_URL).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import matcher, storage  # noqa: E402


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
        actions = matcher.revalidate_split(s, dry_run=not args.commit)
        for a in actions:
            if a["action"] == "dissolve":
                print(f"cl{a['match_id']}: DISSOLVE {a['members']}")
            else:
                print(f"cl{a['match_id']}: KEEP {a['keep']}, EJECT {a['eject']}")
        verb = "COMMITTED" if args.commit else "plan only"
        print(f"\n({verb} — {len(actions)} clusters)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
