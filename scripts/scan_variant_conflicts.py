"""Scan formed clusters for variant/strength ATOM conflicts (analysis only).

Runs matcher on a prod dump, then for every 2+-site cluster extracts "variant
atoms" from each member's RAW name:
  - standalone numbers NOT in pack (N\\d+, №\\d+, \\d+ əd) and NOT dosage (\\d+mg/ml/…)
  - standalone single letters (variant suffixes: M, C, D, …)

Flags a cluster if any cross-site pair has BOTH-sided DIFFERENT atom sets
(symmetric conflict → likely different variant/strength). Prints count + sample
so we can eyeball false-positive risk BEFORE adding the guard to the matcher.

Usage: .venv/bin/python scripts/scan_variant_conflicts.py <dump.json> [--show N]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src import matcher, storage

# Используем ИМЕННО shipped-функции матчера → скан валидирует то, что в проде.
variant_atoms = matcher._variant_atoms  # raw -> frozenset[str]
conflict = matcher._has_conflicting_variant_atoms  # (raw_a, raw_b) -> bool


def main() -> None:
    dump_path = sys.argv[1]
    show = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 40
    with open(dump_path) as f:
        rows = json.load(f)

    engine = create_engine("sqlite://")
    storage.Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    s = Session()
    for r in rows:
        s.add(
            storage.Product(
                id=r["id"],
                tenant_id=1,
                site=r["site"],
                external_id=r["external_id"],
                url=f"http://x/{r['external_id']}",
                name=r["name"] or "",
                name_normalized=r["name_normalized"] or "",
                brand=r["brand"],
                dosage=r["dosage"],
                pack_size=r["pack_size"],
                barcode=r.get("barcode"),
            )
        )
    s.commit()
    matcher.latest_snapshots_per_product = lambda *a, **kw: {}
    matcher.match_products(s)

    members: dict[int, list] = defaultdict(list)
    for p in s.scalars(
        select(storage.Product).where(storage.Product.canonical_id.is_not(None))
    ).all():
        members[p.canonical_id].append(p)

    flagged = []
    for cid, mem in members.items():
        if len({m.site for m in mem}) < 2:
            continue
        bad = any(
            conflict(mem[i].name, mem[j].name)
            for i in range(len(mem))
            for j in range(i + 1, len(mem))
        )
        if bad:
            flagged.append((cid, mem))

    two_plus = sum(1 for mem in members.values() if len({m.site for m in mem}) >= 2)
    print(f"2+ site clusters: {two_plus}")
    print(
        f"clusters with variant/strength ATOM conflict: {len(flagged)} "
        f"({100 * len(flagged) / two_plus:.1f}%)"
    )
    print(f"\n=== SAMPLE {min(show, len(flagged))} flagged (eyeball for FALSE positives) ===")
    for cid, mem in flagged[:show]:
        print(f"\ncluster {cid}:")
        for m in mem:
            print(f"  [{m.site:11}] {m.name[:58]:58} atoms={set(variant_atoms(m.name))}")


if __name__ == "__main__":
    main()
