"""Quantify brand-stub matches: branded product ↔ generic (one-sided sig token).

aptek часто листит generic («Çaytikanı yağı 100ml»), pharm — с брендом
(«Çaytikanı yağı Altay 100ml»). Матчер цепляет их (односторонний уникальный
токен «altay» считается «неполными данными»), давая ложный spread между РАЗНЫМИ
брендами. Этот скан считает, сколько 2+-site кластеров имеют пару с ОДНОСТОРОННИМ
значащим уникальным токеном (len>=4, не noise/form/modifier/descriptor) — т.е.
кандидаты на блокировку более строгим guard'ом. Печатает count + sample.

Usage: .venv/bin/python scripts/scan_brandstub.py <dump.json> [--show N]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src import matcher, storage
from src.matcher import _MATCH_NOISE_TOKENS, _PHARMA_MODIFIERS

# Химические/фарм-дескрипторы — НЕ бренд, тот же товар (терсе ↔ verbose ОК).
_DESCRIPTORS = frozenset(
    {
        "trihydrate",
        "monohydrate",
        "anhydrous",
        "micronized",
        "hcl",
        "sodium",
        "potassium",
        "hydrochloride",
        "sulfate",
        "sulphate",
        "maleate",
        "besylate",
        "tablet",
        "tablets",
        "tabletler",
        "kapsul",
        "kapsulalar",
        "capsule",
        "capsules",
        "mehlul",
        "solution",
        "syrup",
        "serbet",
        "krem",
        "cream",
        "gel",
        "ointment",
        "melhem",
        "drops",
        "damci",
        "sprey",
        "spray",
        "ampul",
        "ampoule",
    }
)


def sig_unique(a: str, b: str) -> set[str]:
    """Значащие токены в a, отсутствующие в b (len>=4, не noise/mod/descriptor)."""
    ta, tb = set(a.split()), set(b.split())
    out = set()
    for t in ta - tb:
        if len(t) < 4:
            continue
        if t in _MATCH_NOISE_TOKENS or t in _PHARMA_MODIFIERS or t in _DESCRIPTORS:
            continue
        out.add(t)
    return out


def main() -> None:
    dump_path = sys.argv[1]
    show = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 30
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
        norms = [(m, m.name_normalized or "") for m in mem]
        # one-sided: какой-то член имеет sig-unique-токен относительно ДРУГОГО,
        # а у того встречного нет sig-unique (т.е. он stub/generic)
        bad = False
        for i in range(len(norms)):
            for j in range(len(norms)):
                if i == j:
                    continue
                ui = sig_unique(norms[i][1], norms[j][1])
                uj = sig_unique(norms[j][1], norms[i][1])
                if ui and not uj:  # i брендовый, j generic
                    bad = True
        if bad:
            flagged.append((cid, mem))

    two_plus = sum(1 for mem in members.values() if len({m.site for m in mem}) >= 2)
    print(f"2+ site clusters: {two_plus}")
    print(
        f"clusters с односторонним sig-токеном (brand-stub риск): {len(flagged)} "
        f"({100 * len(flagged) / two_plus:.1f}%)"
    )
    print(
        f"\n=== SAMPLE {min(show, len(flagged))} (eyeball: bad brand-mismatch или legit terse?) ==="
    )
    for cid, mem in flagged[:show]:
        print(f"\ncluster {cid}:")
        for m in mem:
            print(f"  [{m.site:11}] {m.name[:52]:52} norm={m.name_normalized}")


if __name__ == "__main__":
    main()
