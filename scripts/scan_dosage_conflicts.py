"""Scan formed clusters for DOSAGE conflicts (analysis only).

Secondary/quaternary matcher passes bucket by brand+pack (no dosage) and can
merge products with different labelled dosages (Paracetamol 500 vs 1000mg).
This scans formed clusters: flag if two members have extract_dosage() both
non-null and differing (after _norm_units). Prints count + sample.

Usage: .venv/bin/python scripts/scan_dosage_conflicts.py <dump.json> [--show N]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import re

from src import matcher, storage
from src.normalize import strip_accents

# Только фарм-МАССА действующего вещества: mg/mcg/IU. Исключаем ml (объём),
# g/q (вес косметики/размер), % (концентрация), notation (BV, /Nml) — именно
# они давали ложные конфликты (50000 BV == 50000 IU; 500mg == 500mg/10ml).
_MASS_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(mg|mcg|mkg|mq|iu|tv)\b", re.I)
_UNIT_CANON = {"mq": "mg", "mkg": "mcg", "tv": "iu"}


_THOUSAND_SP_RE = re.compile(r"(\d)\s+(\d{3})(?!\d)")


def dose(raw: str) -> frozenset[str]:
    low = _THOUSAND_SP_RE.sub(r"\1\2", strip_accents(raw or "").lower())  # «1 000»→«1000»
    out = set()
    for num, unit in _MASS_RE.findall(low):
        u = _UNIT_CANON.get(unit, unit)
        out.add(f"{num.replace(',', '.')}{u}")
    return frozenset(out)


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
        doses = [(m, dose(m.name)) for m in mem]
        bad = any(
            da and db and bool(da - db) and bool(db - da)
            for i, (_, da) in enumerate(doses)
            for _, db in doses[i + 1 :]
        )
        if bad:
            flagged.append((cid, mem))

    two_plus = sum(1 for mem in members.values() if len({m.site for m in mem}) >= 2)
    print(f"2+ site clusters: {two_plus}")
    print(f"clusters with DOSAGE conflict: {len(flagged)} ({100 * len(flagged) / two_plus:.1f}%)")
    print(f"\n=== SAMPLE {min(show, len(flagged))} flagged ===")
    for cid, mem in flagged[:show]:
        print(f"\ncluster {cid}:")
        for m in mem:
            print(f"  [{m.site:11}] {m.name[:56]:56} dose={dose(m.name)}")


if __name__ == "__main__":
    main()
