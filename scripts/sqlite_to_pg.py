"""Migrate data from pilot SQLite to production PostgreSQL.

Prerequisites:
  1. Postgres running (docker compose up -d, or remote Hetzner)
  2. DATABASE_URL points to Postgres
  3. `alembic upgrade head` already applied to Postgres (creates schema)

Usage:
  ./.venv/bin/python scripts/sqlite_to_pg.py [--sqlite PATH] [--dry-run]

Strategy:
  - Open both connections (sqlite + postgres)
  - For each table, in dependency order:
      * read all rows from sqlite
      * insert into postgres (skip if PK collision — idempotent re-run)
  - Reset sequences to MAX(id)+1 on each table after insert
  - Validate row counts match

Order matters: parents before children (FKs).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, MetaData, select, func, text, inspect
from sqlalchemy.orm import sessionmaker

# Tables in topological insert order (parents → children)
INSERT_ORDER = [
    "tenants",
    "tenant_users",
    "matches",
    "products",            # FK matches
    "runs",
    "price_snapshots",     # FK runs, products
    "categories",
    "recipients",
    "promos",
    "alert_rules",
    "alert_events",        # FK alert_rules
    "tracked_products",
    "tracked_product_links",  # FK tracked_products
    "match_rejections",    # FK products
    "stock_levels",        # FK products, matches
    "supplier_prices",     # FK products, matches
    "saved_views",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sqlite",
        default="data/db.sqlite",
        help="Path to pilot SQLite (default: data/db.sqlite)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Connect, read counts, but don't insert",
    )
    args = parser.parse_args()

    pg_url = os.environ.get("DATABASE_URL", "")
    if not pg_url.startswith("postgres"):
        print(
            f"ERROR: DATABASE_URL must point to Postgres, got: {pg_url!r}\n"
            "  Set DATABASE_URL=postgresql+psycopg://user:pass@host/db",
            file=sys.stderr,
        )
        return 1

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        print(f"ERROR: SQLite file not found: {sqlite_path}", file=sys.stderr)
        return 1

    sqlite_url = f"sqlite:///{sqlite_path.absolute()}"
    print(f"Source:      {sqlite_url}")
    print(f"Destination: {pg_url}\n")

    src_engine = create_engine(sqlite_url)
    dst_engine = create_engine(pg_url)
    SrcSession = sessionmaker(bind=src_engine)
    DstSession = sessionmaker(bind=dst_engine)

    src_meta = MetaData()
    src_meta.reflect(bind=src_engine)
    dst_meta = MetaData()
    dst_meta.reflect(bind=dst_engine)

    if not dst_meta.tables:
        print(
            "ERROR: Destination DB has no tables. Run `alembic upgrade head` first.",
            file=sys.stderr,
        )
        return 1

    src_session = SrcSession()
    dst_session = DstSession()

    try:
        total_src = 0
        total_dst = 0
        for tbl_name in INSERT_ORDER:
            if tbl_name not in src_meta.tables:
                print(f"  [skip]   {tbl_name:25s} — not in source")
                continue
            if tbl_name not in dst_meta.tables:
                print(f"  [skip]   {tbl_name:25s} — not in destination")
                continue

            src_tbl = src_meta.tables[tbl_name]
            dst_tbl = dst_meta.tables[tbl_name]

            rows = src_session.execute(select(src_tbl)).mappings().all()
            src_count = len(rows)
            total_src += src_count

            if args.dry_run:
                print(f"  [dry-run] {tbl_name:25s} {src_count:>8} rows")
                continue

            if src_count == 0:
                print(f"  [empty]   {tbl_name:25s}")
                continue

            # Filter row keys to only columns that exist on destination
            dst_cols = {c.name for c in dst_tbl.columns}
            cleaned = [{k: v for k, v in r.items() if k in dst_cols} for r in rows]

            # Bulk insert with on-conflict-do-nothing for idempotency.
            # Chunked: Postgres max params per query is 65535. With ~20 cols per row,
            # safe chunk = 65535 / cols → cap at 1000 rows for headroom.
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            n_cols = len(dst_tbl.columns)
            chunk_size = max(1, min(1000, 65535 // max(1, n_cols)))
            pk_cols = [c.name for c in dst_tbl.primary_key.columns]
            for i in range(0, len(cleaned), chunk_size):
                chunk = cleaned[i : i + chunk_size]
                stmt = pg_insert(dst_tbl).values(chunk)
                stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
                dst_session.execute(stmt)
            dst_session.commit()
            inserted = dst_session.execute(
                select(func.count()).select_from(dst_tbl)
            ).scalar()
            total_dst += inserted
            print(f"  [migrate] {tbl_name:25s} src={src_count:>6} → dst={inserted:>6}")

        if not args.dry_run:
            # Reset Postgres sequences to MAX(id)+1
            print("\nResetting sequences...")
            insp = inspect(dst_engine)
            for tbl_name in INSERT_ORDER:
                if tbl_name not in dst_meta.tables:
                    continue
                tbl = dst_meta.tables[tbl_name]
                if "id" not in tbl.columns:
                    continue
                seq_name = f"{tbl_name}_id_seq"
                # Check if sequence exists (Postgres autoincrement uses sequences)
                with dst_engine.connect() as conn:
                    res = conn.execute(
                        text(
                            "SELECT 1 FROM pg_class WHERE relkind='S' AND relname=:n"
                        ),
                        {"n": seq_name},
                    ).scalar()
                    if res:
                        max_id = conn.execute(
                            text(f"SELECT COALESCE(MAX(id), 0) FROM {tbl_name}")
                        ).scalar()
                        conn.execute(
                            text(f"SELECT setval(:s, :v)"),
                            {"s": seq_name, "v": max_id + 1},
                        )
                        conn.commit()
                        print(f"  {seq_name:35s} → {max_id + 1}")

        print(f"\nTotal source rows: {total_src}")
        print(f"Total dest rows  : {total_dst}")
        print("\n✓ Migration complete" if not args.dry_run else "\n(dry-run)")
        return 0
    finally:
        src_session.close()
        dst_session.close()


if __name__ == "__main__":
    sys.exit(main())
