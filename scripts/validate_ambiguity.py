"""Validate ambiguity-suppression + strength guard on a prod dump.

Runs matcher, then for given name-substrings prints the resulting clusters so we
can confirm: (a) Çaytikanı cross-brand gone, (b) Mikrazim 25000≠10000 split,
(c) legit generics (Nestogen/Doksisiklin) preserved.

Usage: .venv/bin/python scripts/validate_ambiguity.py <dump.json>
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src import matcher, storage

CHECKS = ["aytikan", "mikrazim", "nestogen", "doksisiklin", "lorinden"]


def main() -> None:
    with open(sys.argv[1]) as f:
        rows = json.load(f)
    engine = create_engine("sqlite://")
    storage.Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    s = Session()
    for r in rows:
        s.add(
            storage.Product(
                id=r["id"], tenant_id=1, site=r["site"], external_id=r["external_id"],
                url=f"http://x/{r['external_id']}", name=r["name"] or "",
                name_normalized=r["name_normalized"] or "", brand=r["brand"],
                dosage=r["dosage"], pack_size=r["pack_size"], barcode=r.get("barcode"),
            )
        )
    s.commit()
    matcher.latest_snapshots_per_product = lambda *a, **kw: {}
    matcher.match_products(s)

    total = s.scalar(select(func.count(storage.Match.id)))
    print(f"total matches: {total}")

    for kw in CHECKS:
        prods = s.scalars(
            select(storage.Product).where(storage.Product.name.ilike(f"%{kw}%"))
        ).all()
        clusters: dict[int, list] = defaultdict(list)
        unmatched = 0
        for p in prods:
            if p.canonical_id:
                clusters[p.canonical_id].append(p)
            else:
                unmatched += 1
        multi = {c: m for c, m in clusters.items() if len({x.site for x in m}) >= 2}
        print(f"\n=== '{kw}': {len(prods)} продуктов, {len(multi)} cross-site кластеров, {unmatched} unmatched ===")
        for c, mem in list(multi.items())[:6]:
            sites = " | ".join(f"{m.site[:4]}:{m.name[:30]}" for m in mem)
            print(f"  c{c}: {sites}")


if __name__ == "__main__":
    main()
