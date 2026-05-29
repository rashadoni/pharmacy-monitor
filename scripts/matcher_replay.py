"""One-off: replay matcher against a prod products dump in isolated in-memory DB.

Validates matcher coverage changes WITHOUT touching prod. Loads products from
a JSON dump (id, site, external_id, name, name_normalized, brand, dosage,
pack_size, barcode), runs match_products(), reports:
  - matches with 2+ sites (the comparison-eligible count)
  - random sample of clusters for manual false-positive audit

Usage:
    .venv/bin/python scripts/matcher_replay.py <dump.json> [--sample N]
"""

from __future__ import annotations

import json
import sys

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src import matcher, storage


def main() -> None:
    dump_path = sys.argv[1]
    sample_n = 25
    if "--sample" in sys.argv:
        sample_n = int(sys.argv[sys.argv.index("--sample") + 1])

    with open(dump_path) as f:
        rows = json.load(f)

    # --renormalize: пересчитать name_normalized из сырого name с ТЕКУЩИМ
    # normalize_name (fixed strip_accents ə→e/ı→i). Симулирует прод-backfill,
    # чтобы измерить эффект accent-fix на coverage до деплоя.
    renorm = "--renormalize" in sys.argv
    if renorm:
        from src.normalize import normalize_name

        for r in rows:
            r["name_normalized"] = normalize_name(r["name"] or "")
        print("[--renormalize] recomputed name_normalized from raw name")

    # Isolated in-memory SQLite
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
    print(f"loaded {len(rows)} products")

    # No snapshots in replay → patch price lookup to {} (SQLite chokes on
    # IN(57k ids), and per-unit check is moot without prices). _has_perunit_mismatch
    # with empty dict returns False (no block) — correct for coverage measurement.
    matcher.latest_snapshots_per_product = lambda *a, **kw: {}

    # --old-disparity: monkeypatch the guard back to pre-2026-05-29 pure-ratio
    # logic to measure the recall delta from the stub-aware redesign.
    if "--old-disparity" in sys.argv:

        def _old_guard(name_a: str, name_b: str, brand_hint: str = "") -> bool:
            wa, wb = len(name_a.split()), len(name_b.split())
            if wa == 0 or wb == 0:
                return False
            longer = max(wa, wb)
            if longer < 3:
                return False
            return min(wa, wb) / longer < 0.40

        matcher._has_extreme_length_disparity = _old_guard
        print("[--old-disparity] using pre-fix pure-ratio guard")

    created = matcher.match_products(s)
    print(f"match_products created/updated: {created}")

    # Count clusters by site-count
    from collections import Counter, defaultdict

    cluster_sites: dict[int, set[str]] = defaultdict(set)
    prods = s.scalars(select(storage.Product).where(storage.Product.canonical_id.is_not(None))).all()
    for p in prods:
        cluster_sites[p.canonical_id].add(p.site)
    dist = Counter(len(sites) for sites in cluster_sites.values())
    two_plus = sum(n for k, n in dist.items() if k >= 2)
    total_matches = s.scalar(select(func.count(storage.Match.id)))
    matched_products = s.scalar(
        select(func.count(storage.Product.id)).where(storage.Product.canonical_id.is_not(None))
    )

    print(f"\n=== RESULTS ===")
    print(f"total matches:        {total_matches}")
    print(f"matched products:     {matched_products}")
    print(f"clusters by sites:    {dict(sorted(dist.items()))}")
    print(f"matches with 2+ sites: {two_plus}  <-- comparison-eligible")

    # Sample clusters for audit (2+ sites only)
    multi = [cid for cid, sites in cluster_sites.items() if len(sites) >= 2]
    import random

    random.seed(42)
    sample = random.sample(multi, min(sample_n, len(multi)))
    print(f"\n=== RANDOM SAMPLE of {len(sample)} multi-site clusters (audit for FALSE POSITIVES) ===")
    for cid in sample:
        members = s.scalars(
            select(storage.Product).where(storage.Product.canonical_id == cid)
        ).all()
        print(f"\ncluster {cid}:")
        for m in members:
            print(f"  [{m.site:11}] {m.name[:70]}")


if __name__ == "__main__":
    main()
