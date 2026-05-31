#!/usr/bin/env python3
"""Re-extract pack_size (and optionally dosage/name_normalized) over ALL existing
products, using the current (fixed) extractors.

Why: the concentration-shadow fix in extract_pack_size (strip "X/Yml" before the
volume regex) only ran for NEW scrapes — existing rows still store the concentration
denominator (e.g. "5ml" from "200mq/5ml") as pack_size. That stale value:
  - MERGES wrong clusters (two different bottle volumes share pack="5ml") → precision
  - SPLITS true twins (one site fixed to "100ml", other stale "5ml") → recall
Recomputing pack_size for all rows makes the (brand,dosage,pack) bucket key consistent
so a follow-up match_products re-pairs the split twins and revalidate splits the merges.

Deterministic correction (no network). Dry-run by default; --commit applies.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from src import storage  # noqa: E402
from src.normalize import extract_dosage, extract_pack_size, normalize_name  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--also-dosage", action="store_true", help="also recompute dosage")
    ap.add_argument("--also-name", action="store_true", help="also recompute name_normalized")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    db = os.environ.get("DATABASE_URL")
    if not db:
        print("ERROR: set DATABASE_URL", file=sys.stderr)
        return 2
    Session = storage.make_session(db)
    with Session() as s:
        prods = s.scalars(select(storage.Product)).all()
        n_pack = n_dose = n_name = 0
        samples = []
        for p in prods:
            new_pack = extract_pack_size(p.name)
            if new_pack != p.pack_size:
                n_pack += 1
                if len(samples) < 12:
                    samples.append((p.pack_size, new_pack, (p.name or "").strip()[:42]))
                if args.commit:
                    p.pack_size = new_pack
            if args.also_dosage:
                nd = extract_dosage(p.name)
                if nd != p.dosage:
                    n_dose += 1
                    if args.commit:
                        p.dosage = nd
            if args.also_name:
                nn = normalize_name(p.name)
                if nn != p.name_normalized:
                    n_name += 1
                    if args.commit:
                        p.name_normalized = nn
        if args.commit:
            s.commit()
        verb = "SET" if args.commit else "would change"
        print(f"products: {len(prods)}")
        print(f"  pack_size {verb}: {n_pack}")
        if args.also_dosage:
            print(f"  dosage {verb}: {n_dose}")
        if args.also_name:
            print(f"  name_normalized {verb}: {n_name}")
        print("--- pack_size samples (old → new | name) ---")
        for old, new, nm in samples:
            print(f"   {str(old):<8} → {str(new):<8} | {nm}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
