#!/usr/bin/env python3
"""Full from-scratch re-match (mirror of `rematch --reset` + auto-revalidate).

Resets all AUTO canonical_id, deletes AUTO Match rows (is_manual=True preserved),
re-normalizes names, re-clusters via match_products (respects ALL match_rejections
+ current guards incl. strict commodity brand/country/grade), then revalidate_split
to apply the conflict guards that primary passes don't. Manual matches untouched.

Heavy + destructive. NOT for production: it deletes every automatic pair, so
all pair ids change and a from-scratch build does not reproduce the current
pairs (measured 2026-10-07, see docs/RUNBOOK.md «Выкладка правок сопоставления»).
It takes neither the run lock nor the matcher lock. Use it on a copy of the
database only; like `pharmacy-monitor rematch --reset` it refuses to run
without the explicit `--i-accept-full-rebuild` argument.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete as sa_delete  # noqa: E402
from sqlalchemy import distinct, func, select  # noqa: E402
from sqlalchemy import update as sa_update  # noqa: E402

from src import matcher, storage  # noqa: E402
from src.normalize import normalize_name  # noqa: E402


CONFIRM_FLAG = "--i-accept-full-rebuild"


def main() -> int:
    if CONFIRM_FLAG not in sys.argv[1:]:
        print(
            f"ERROR: this deletes every automatic pair; not for production. "
            f"On a database copy pass {CONFIRM_FLAG}.",
            file=sys.stderr,
        )
        return 2
    db = os.environ.get("DATABASE_URL")
    if not db:
        print("ERROR: set DATABASE_URL", file=sys.stderr)
        return 2
    Session = storage.make_session(db)
    with Session() as s:
        manual = s.scalar(
            select(func.count(storage.Match.id)).where(storage.Match.is_manual.is_(True))
        )
        auto_ids = s.scalars(
            select(storage.Match.id).where(storage.Match.is_manual.is_(False))
        ).all()
        print(f"manual matches preserved: {manual} | auto matches to reset: {len(auto_ids)}")
        if auto_ids:
            s.execute(
                sa_update(storage.Product)
                .where(storage.Product.canonical_id.in_(auto_ids))
                .values(canonical_id=None)
            )
            s.execute(sa_delete(storage.Match).where(storage.Match.is_manual.is_(False)))
            s.commit()
        # re-normalize names with current pipeline
        prods = s.scalars(select(storage.Product)).all()
        for p in prods:
            p.name_normalized = normalize_name(p.name or "")
        s.commit()
        print(f"re-normalized {len(prods)} products; clustering…")
        clusters = matcher.match_products(s)
        print(f"match_products: {clusters} clusters")
        actions = matcher.revalidate_split(s, dry_run=False)
        print(f"revalidate_split: re-split {len(actions)} clusters (+rejections)")
        # report final shape
        matched = s.scalar(
            select(func.count(storage.Product.id)).where(
                storage.Product.canonical_id.is_not(None)
            )
        )
        cross = s.scalar(
            select(func.count()).select_from(
                select(storage.Product.canonical_id)
                .where(storage.Product.canonical_id.is_not(None))
                .group_by(storage.Product.canonical_id)
                .having(func.count(distinct(storage.Product.site)) >= 2)
                .subquery()
            )
        )
        print(f"FINAL: matched_products={matched} cross_site_clusters={cross}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
