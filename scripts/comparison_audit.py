"""Audit comparison-row quality on a prod dump. Categorizes defects so we can
prioritize fixes for the price-comparison feature.

Inputs: cmp_matches.json (clusters+products), cmp_prices.json (pid→price).
Detects:
  - per-unit/per-pack artifacts: price ratio ≈ pack-count ratio (false big spread)
  - extreme spread (>=50%) breakdown: per-unit vs genuine vs parse-error
  - parse-error suspects: price > 5000 or < 0.1
  - identical-name-but-different price (true comparable)
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter

TMP = os.environ["TMPDIR"]
matches = json.load(open(f"{TMP}/cmp_matches.json"))
prices = {r["pid"]: r["price"] for r in json.load(open(f"{TMP}/cmp_prices.json"))}


def pack_count(pack: str | None, name: str | None) -> float:
    """Extract pack count (N10→10). Falls back to name 'N10'/'№10'. Default 1."""
    for src in (pack or "", name or ""):
        m = re.search(r"[n№]\s*(\d{1,3})\b", src, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return 1.0


rows_with_2plus_prices = 0
spread_buckets = Counter()
perunit_artifacts = []
parse_errors = []
genuine_big_spread = []

for m in matches:
    priced = []
    for p in m["prods"]:
        pr = prices.get(p["pid"])
        if pr and pr > 0:
            priced.append({**p, "price": pr})
    # dedupe by site (take cheapest per site, like the endpoint)
    by_site: dict[str, dict] = {}
    for p in priced:
        if p["site"] not in by_site or p["price"] < by_site[p["site"]]["price"]:
            by_site[p["site"]] = p
    priced = list(by_site.values())
    if len(priced) < 2:
        continue
    rows_with_2plus_prices += 1

    vals = [p["price"] for p in priced]
    lo, hi = min(vals), max(vals)
    spread = (hi - lo) / hi * 100 if hi else 0
    if spread < 10:
        bucket = "0-10%"
    elif spread < 30:
        bucket = "10-30%"
    elif spread < 50:
        bucket = "30-50%"
    elif spread < 90:
        bucket = "50-90%"
    else:
        bucket = "90%+"
    spread_buckets[bucket] += 1

    # parse-error suspects
    if hi > 5000 or lo < 0.1:
        parse_errors.append((m["canonical_name"], {p["site"]: p["price"] for p in priced}))
        continue

    if spread >= 50:
        # per-unit check: normalize price by pack_count, recompute spread
        norm = [p["price"] / pack_count(p["pack_size"], p["name"]) for p in priced]
        nlo, nhi = min(norm), max(norm)
        nspread = (nhi - nlo) / nhi * 100 if nhi else 0
        if nspread < 25:  # spread collapses after per-unit normalization
            perunit_artifacts.append(
                (
                    m["canonical_name"],
                    {
                        p["site"]: (p["price"], pack_count(p["pack_size"], p["name"]))
                        for p in priced
                    },
                    round(spread),
                    round(nspread),
                )
            )
        else:
            genuine_big_spread.append(
                (m["canonical_name"], {p["site"]: p["price"] for p in priced}, round(spread))
            )

print(f"=== comparison rows with 2+ prices: {rows_with_2plus_prices} ===\n")
print("spread distribution:")
for k in ("0-10%", "10-30%", "30-50%", "50-90%", "90%+"):
    print(f"  {k:8} {spread_buckets[k]}")
print(f"\nparse-error suspects (price>5000 or <0.1): {len(parse_errors)}")
for nm, pr in parse_errors[:8]:
    print(f"  {nm[:45]:45} {pr}")
print(f"\nPER-UNIT ARTIFACTS (spread collapses after /pack_count): {len(perunit_artifacts)}")
for nm, detail, sp, nsp in sorted(perunit_artifacts, key=lambda x: -x[2])[:12]:
    print(f"  {sp:3}%→{nsp:2}%  {nm[:42]:42} {detail}")
print(f"\nGENUINE big spread (>=50%, survives per-unit): {len(genuine_big_spread)}")
for nm, pr, sp in sorted(genuine_big_spread, key=lambda x: -x[2])[:8]:
    print(f"  {sp:3}%  {nm[:45]:45} {pr}")
