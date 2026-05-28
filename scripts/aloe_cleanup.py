"""Task #33 cleanup — migrate old listing-URL aloe products to new slug-based rows.

Usage:
    python aloe-cleanup.py --dry-run   # report only, no mutations (DEFAULT in this script)
    python aloe-cleanup.py --apply     # really do the migration

After deploy of aloe.py fix, new scrape creates new rows with proper detail URLs.
Old rows with listing URLs + old-style external_ids остаются — ~1534 zombies.

Strategy per old row:
  - Compute new_slug = aloe_slug(name)
  - If new row exists (site='aloe', external_id=new_slug):
      * Migrate price_snapshots.product_id from old → new
      * Re-point canonical_id (match cluster) on new if new lacks one
      * Delete old row
  - Else: UPDATE old in-place (set url+external_id), preserve all FKs

Idempotent — running twice is safe (zombies count drops to 0 second run, no-op).
"""
import argparse
import sys

sys.path.insert(0, "/opt/pharmacy-monitor")

from src import storage
from src.scrapers.aloe import aloe_slug
from sqlalchemy import select, text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Default mode — report only, no mutations")
    parser.add_argument("--apply", action="store_true",
                        help="Actually apply mutations (overrides --dry-run)")
    args = parser.parse_args()
    apply = args.apply
    mode = "APPLY" if apply else "DRY-RUN"

    storage.init_db()
    Session = storage.make_session()

    would_merge = 0   # has new row → delete old, migrate FKs
    would_update_inplace = 0  # no new row → keep old row, just fix url+external_id
    would_skip_failed_slug = 0
    snapshots_to_migrate = 0
    matches_inherited = 0

    samples_merge = []
    samples_inplace = []

    with Session() as s:
        all_aloe = s.execute(
            select(storage.Product).where(storage.Product.site == "aloe")
        ).scalars().all()

        by_external_id = {p.external_id: p for p in all_aloe}
        old_listing_rows = [
            p for p in all_aloe
            if p.url and "catalog/filters" in p.url
        ]
        print(f"[{mode}] Total aloe products: {len(all_aloe)}")
        print(f"[{mode}] Old listing-URL rows to process: {len(old_listing_rows)}")
        print()

        for old in old_listing_rows:
            new_slug = aloe_slug(old.name)
            if not new_slug:
                would_skip_failed_slug += 1
                continue

            new_row = by_external_id.get(new_slug)
            if new_row and new_row.id != old.id:
                would_merge += 1
                # Count snapshots that would be migrated
                snap_count = s.scalar(text(
                    "SELECT COUNT(*) FROM price_snapshots WHERE product_id=:pid"
                ), {"pid": old.id})
                snapshots_to_migrate += snap_count or 0
                if old.canonical_id and not new_row.canonical_id:
                    matches_inherited += 1
                if len(samples_merge) < 3:
                    samples_merge.append(
                        f"  MERGE old.id={old.id} '{old.name[:40]}' "
                        f"→ new.id={new_row.id} ({snap_count} snapshots, "
                        f"canonical={'inherit' if old.canonical_id and not new_row.canonical_id else 'no-op'})"
                    )

                if apply:
                    s.execute(
                        text("UPDATE price_snapshots SET product_id=:new_id WHERE product_id=:old_id"),
                        {"new_id": new_row.id, "old_id": old.id},
                    )
                    if old.canonical_id and not new_row.canonical_id:
                        new_row.canonical_id = old.canonical_id
                    old.canonical_id = None
                    s.flush()
                    s.delete(old)
                    s.flush()
            else:
                would_update_inplace += 1
                if len(samples_inplace) < 3:
                    samples_inplace.append(
                        f"  IN-PLACE old.id={old.id} '{old.name[:40]}' "
                        f"→ url=https://aloe.az/{new_slug}/"
                    )
                if apply:
                    old.url = f"https://aloe.az/{new_slug}/"
                    if new_slug not in by_external_id:
                        old.external_id = new_slug
                        by_external_id[new_slug] = old

        if apply:
            s.commit()
        else:
            s.rollback()  # belt-and-suspenders — make sure nothing leaks

    print(f"=== {mode} SUMMARY ===")
    print(f"  Would MERGE (delete old + migrate FKs): {would_merge}")
    print(f"  Would UPDATE in-place (no new row): {would_update_inplace}")
    print(f"  Would SKIP (empty slug): {would_skip_failed_slug}")
    print()
    print(f"  Snapshots to migrate: {snapshots_to_migrate}")
    print(f"  Matches inheritance changes: {matches_inherited}")
    print()
    if samples_merge:
        print("Sample MERGE operations:")
        for s_ in samples_merge:
            print(s_)
    if samples_inplace:
        print()
        print("Sample IN-PLACE updates:")
        for s_ in samples_inplace:
            print(s_)

    if not apply:
        print()
        print("=== DRY-RUN COMPLETE — no changes made. Re-run with --apply to commit. ===")


if __name__ == "__main__":
    main()
