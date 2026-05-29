"""Validate matcher dosage/variant guards against a prod dump (isolated SQLite).

Runs match_products() on the dump (NO MatchRejection rows → pure matcher logic),
then for a list of (id_a, id_b, expected) pairs reports whether they landed in
the SAME cluster or got SPLIT. Also prints total 2+-site match count as an
over-blocking regression metric.

Usage:
    .venv/bin/python scripts/validate_dosage_variant.py <dump.json>
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src import matcher, storage

# (id_a, id_b, expectation) — "split" = должны быть в РАЗНЫХ кластерах (wrong-match),
# "same" = должны остаться вместе (настоящий матч; guard против over-block).
PAIRS: list[tuple[int, int, str]] = [
    (50185, 23832, "split"),  # Normoqlip M  ≠ Normoqlip 2 (вариант)
    (53622, 13038, "split"),  # Mikrazim 25000 ED ≠ Mikrazim 10000 (доза)
    (46685, 55636, "split"),  # Aspirin C ≠ Aspirin (aloe)
    (46685, 12417, "split"),  # Aspirin C ≠ Asetilsalisil (aptek)
    (55636, 12417, "same"),  # Aspirin (aloe) == Asetilsalisil aspirin (aptek) — НАСТОЯЩИЙ
]


def main() -> None:
    dump_path = sys.argv[1]
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
    # No snapshots in replay → per-unit check moot.
    matcher.latest_snapshots_per_product = lambda *a, **kw: {}

    matcher.match_products(s)

    canon: dict[int, int | None] = {
        p.id: p.canonical_id for p in s.scalars(select(storage.Product)).all()
    }
    cluster_sites: dict[int, set[str]] = defaultdict(set)
    for p in s.scalars(
        select(storage.Product).where(storage.Product.canonical_id.is_not(None))
    ).all():
        cluster_sites[p.canonical_id].add(p.site)
    dist = Counter(len(v) for v in cluster_sites.values())
    two_plus = sum(n for k, n in dist.items() if k >= 2)
    total = s.scalar(select(func.count(storage.Match.id)))

    print(f"total matches:        {total}")
    print(f"matches with 2+ sites: {two_plus}  <-- comparison-eligible (regression metric)")
    print("\n=== PAIR CHECKS ===")
    ok = 0
    for a, b, exp in PAIRS:
        ca, cb = canon.get(a), canon.get(b)
        together = ca is not None and ca == cb
        status = "same" if together else "split"
        verdict = "OK " if status == exp else "FAIL"
        if status == exp:
            ok += 1
        print(f"  [{verdict}] {a} vs {b}: expected={exp:5} got={status:5} (canon {ca} / {cb})")
    print(f"\n{ok}/{len(PAIRS)} pair expectations met")


if __name__ == "__main__":
    main()
