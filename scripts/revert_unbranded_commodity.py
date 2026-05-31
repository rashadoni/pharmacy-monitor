#!/usr/bin/env python3
"""Revert recall-applied unbranded-commodity cross-site clusters.

My recall pass (recall_candidates.py --ultra/--attach, confidence=0.9) auto-linked
commodity products (botanical oils/teas/extracts) via ultra_equal, which had no
brand axis. For commodity items the brand IS the differentiator, but brand_verified
is NULL on most of them and not recoverable from the slug — so cross-brand oils
(Çaytikanı Mirrolla vs Fitooil/Herba Flora) got welded together and the brand guard
can't split them (no brand data).

This undoes exactly that over-reach: AUTO (is_manual=False) cross-site Match rows
with confidence≈0.9 whose members are ALL commodity-named AND lack a resolvable
consumer brand on ≥2 distinct sites (so we cannot confirm same product). Such
clusters are dissolved (canonical_id=None + Match deleted) and the products return
to the /matcher human queue — where a person confirms the real twins. We do NOT
add a rejection (the correct ones must remain re-linkable).

Manually-confirmed matches (is_manual=True) and brand-known clusters are untouched.
Dry-run by default; --commit applies. Run on prod (DATABASE_URL via peer/tunnel).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from src import storage  # noqa: E402
from src.brand_resolver import consumer_brand, is_commodity_name  # noqa: E402


def _unbranded_commodity_cluster(members: list) -> bool:
    """True if every member is a commodity name AND we cannot confirm the cluster
    via brand: fewer than 2 DISTINCT sites carry a resolvable consumer brand."""
    if not members:
        return False
    if not all(is_commodity_name(p.name) for p in members):
        return False
    sites_with_brand = {
        p.site for p in members if consumer_brand(getattr(p, "brand_verified", None))
    }
    return len(sites_with_brand) < 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--conf",
        type=float,
        default=0.9,
        help="only target Match.confidence == this (recall signature); 0 = any auto",
    )
    ap.add_argument("--commit", action="store_true", help="apply (default: plan only)")
    ap.add_argument("--sample", type=int, default=40)
    args = ap.parse_args()
    db = os.environ.get("DATABASE_URL")
    if not db:
        print("ERROR: set DATABASE_URL", file=sys.stderr)
        return 2
    Session = storage.make_session(db)
    with Session() as s:
        matches = s.scalars(
            select(storage.Match).where(storage.Match.is_manual.is_(False))
        ).all()
        targets = []
        for m in matches:
            members = list(m.products)
            if len({p.site for p in members}) < 2:
                continue  # not cross-site
            if args.conf and m.confidence is not None and abs(m.confidence - args.conf) > 1e-6:
                continue
            if _unbranded_commodity_cluster(members):
                targets.append((m, members))

        print(f"AUTO cross-site matches scanned: {len(matches)}")
        print(
            f"UNBRANDED-COMMODITY clusters to dissolve (conf={args.conf or 'any'}): {len(targets)}"
        )
        print("--- sample (members) — EYEBALL ---")
        for m, members in targets[: args.sample]:
            label = " | ".join(f"{p.site[:3]}:{(p.name or '').strip()[:30]}" for p in members)
            print(f"  cl{m.id}: {label}")

        if args.commit:
            dissolved = 0
            for m, members in targets:
                for p in members:
                    p.canonical_id = None
                s.delete(m)
                dissolved += 1
            s.commit()
            print(f"\nCOMMITTED: dissolved {dissolved} clusters → members back to /matcher queue")
        else:
            print(f"\n(plan only — {len(targets)} would dissolve; pass --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
