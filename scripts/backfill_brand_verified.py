#!/usr/bin/env python3
"""Backfill products.brand_verified from authoritative sources.

Run from Mac (Baku IP — aptek doesn't ban it) against the prod DB via the SSH
tunnel, e.g.:

    DATABASE_URL='postgresql+psycopg://pm:PASS@localhost:5433/pharmacy_monitor' \
      .venv/bin/python scripts/backfill_brand_verified.py --scope cluster-members

Sources:
  - aloe        → products.brand (already the real firm; bulk copy)
  - pharmonline → brand token parsed from the URL slug (page is a JS SPA)
  - aptekonline → the ``"brand":{"id":N,"name":"…"}`` JSON embedded in the
                  product page HTML (the category API omits brand). Throttled,
                  circuit-broken, resumable (skips rows already filled).

brand_verified is ADDITIVE metadata — populating it changes nothing until the
matcher's brand-conflict guard re-clusters. Safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import codecs
import os
import re
import sys
import time

import httpx
from sqlalchemy import select, update

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import storage  # noqa: E402
from src.brand_resolver import brand_from_pharmonline_slug  # noqa: E402

# aptek product page embeds: "brand":{"id":13,"user_id":2,"name":"Biola",...}
_APT_BRAND_RE = re.compile(r'"brand":\{"id":\d+,"user_id":\d+,"name":"([^"]+)"')
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _decode(s: str) -> str:
    """Decode JSON \\uXXXX escapes that leak through the regex (e.g. Vega \\u0130la\\u00e7)."""
    try:
        return codecs.decode(s, "unicode_escape").encode("latin1").decode("utf-8")
    except Exception:
        try:
            return codecs.decode(s, "unicode_escape")
        except Exception:
            return s


def _aptek_brand(client: httpx.Client, url: str) -> str | None:
    r = client.get(url, headers=_HEADERS, timeout=20.0, follow_redirects=True)
    if r.status_code != 200:
        raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
    m = _APT_BRAND_RE.search(r.text)
    return _decode(m.group(1)).strip() if m else None


def _cluster_member_filter():
    """Products that are members of a cross-site cluster (the only rows the
    revalidate re-cluster examines)."""
    from sqlalchemy import distinct, func

    sub = (
        select(storage.Product.canonical_id)
        .where(storage.Product.canonical_id.is_not(None))
        .group_by(storage.Product.canonical_id)
        .having(func.count(distinct(storage.Product.site)) >= 2)
    )
    return storage.Product.canonical_id.in_(sub)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["cluster-members", "all"], default="cluster-members")
    ap.add_argument("--limit", type=int, default=None, help="cap aptek fetches (testing)")
    ap.add_argument("--delay", type=float, default=0.4, help="seconds between aptek fetches")
    ap.add_argument("--breaker", type=int, default=12, help="abort after N consecutive errors")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = os.environ.get("DATABASE_URL")
    if not db:
        print("ERROR: set DATABASE_URL (prod via tunnel)", file=sys.stderr)
        return 2
    Session = storage.make_session(db)

    member_filter = _cluster_member_filter()

    # ── aloe: brand is already the real firm ────────────────────────────────
    with Session() as s:
        q = update(storage.Product).where(
            storage.Product.site == "aloe",
            storage.Product.brand_verified.is_(None),
            storage.Product.brand.is_not(None),
        )
        if args.scope == "cluster-members":
            q = q.where(member_filter)
        q = q.values(brand_verified=storage.Product.brand)
        if not args.dry_run:
            res = s.execute(q)
            s.commit()
            print(f"aloe: set brand_verified for {res.rowcount} rows (= brand)")
        else:
            print("aloe: (dry-run) would copy brand → brand_verified")

    # ── pharmonline: parse brand from slug ──────────────────────────────────
    with Session() as s:
        cond = [storage.Product.site == "pharmonline", storage.Product.brand_verified.is_(None)]
        if args.scope == "cluster-members":
            cond.append(member_filter)
        rows = s.scalars(select(storage.Product).where(*cond)).all()
        filled = 0
        for p in rows:
            b = brand_from_pharmonline_slug(p.url)
            if b:
                if not args.dry_run:
                    p.brand_verified = b
                filled += 1
        if not args.dry_run:
            s.commit()
        print(f"pharmonline: parsed brand from slug for {filled}/{len(rows)} rows")

    # ── aptekonline: fetch product page, extract embedded brand JSON ─────────
    with Session() as s:
        cond = [
            storage.Product.site == "aptekonline",
            storage.Product.brand_verified.is_(None),
            storage.Product.url_dead_at.is_(None),  # dead pages → 404, no brand JSON
        ]
        if args.scope == "cluster-members":
            cond.append(member_filter)
        apt = s.scalars(select(storage.Product).where(*cond)).all()
    total = len(apt)
    if args.limit:
        apt = apt[: args.limit]
    print(f"aptekonline: {total} rows need brand (fetching {len(apt)}; delay={args.delay}s)")
    if args.dry_run:
        print("aptekonline: (dry-run) skipping fetches")
        return 0

    consecutive_err = 0
    ok = miss = err = 0
    with httpx.Client() as client, Session() as s:
        for i, p in enumerate(apt, 1):
            try:
                brand = _aptek_brand(client, p.url)
                consecutive_err = 0
                if brand:
                    db_p = s.get(storage.Product, p.id)
                    if db_p is not None:
                        db_p.brand_verified = brand
                    ok += 1
                else:
                    miss += 1
            except Exception as e:  # noqa: BLE001
                err += 1
                consecutive_err += 1
                if consecutive_err >= args.breaker:
                    s.commit()
                    print(
                        f"\nCIRCUIT BREAKER: {consecutive_err} consecutive errors "
                        f"(last: {type(e).__name__}). Aborting — re-run to resume.",
                        file=sys.stderr,
                    )
                    return 3
            if i % 100 == 0:
                s.commit()
                print(f"  …{i}/{len(apt)}  ok={ok} miss={miss} err={err}")
            time.sleep(args.delay)
        s.commit()
    print(f"aptekonline done: ok={ok} miss={miss} err={err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
