-- Dedup pharmonline Playwright-slug duplicates (2026-06-16)
-- =========================================================
-- ROOT CAUSE: pharmonline was scraped via TWO paths with DIFFERENT external_id
-- keys, each creating its own product row per item:
--   * server-DDP      → external_id = Meteor _id  (17-char [A-Za-z0-9])
--   * Mac-Playwright  → external_id = URL slug     (hyphenated, longer)
-- pharmonline moved to server-DDP-only, so the Playwright slug rows стали
-- осиротевшими (0 refreshed by run_274). 9783 slug rows have a FRESH hash twin
-- (same url, last_seen < 2d). This inflated the catalog 9840→19811 and made the
-- health site_drop metric read 49% (false). Confirmed: 9784/9971 slug rows share
-- a url with a fresh hash row; 9703/9971 share a name; 0 are <30d... wait — all
-- 2-10d old (recent dupes, not long stale).
--
-- STRATEGY (conservative — 0 clusters lose pharmonline):
--   delete a slug row ONLY if (a) it is unclustered, OR (b) it is clustered AND
--   its fresh twin is unclustered (we move the cluster membership to the twin
--   FIRST). Slug rows whose twin already sits in a DIFFERENT cluster (305) are
--   KEPT — deleting them would orphan a cluster's pharmonline side; they self-heal
--   (age out of the 21d freshness window + a future full rematch). The 77 slug
--   rows with NO fresh twin are KEPT (may be Playwright-only / genuinely delisted).
--
-- Expected: ~9478 deleted (8579 unclustered + 899 membership-moved), 388 kept.
-- Fully reversible via the backup tables created below.

BEGIN;

-- Serialize against concurrent scrape-persist / matcher writes for the txn's
-- (sub-second) duration. SHARE ROW EXCLUSIVE blocks DML on these tables but NOT
-- reads (the /comparison API keeps working). lock_timeout → abort rather than
-- block prod writers indefinitely if a long matcher holds ROW EXCLUSIVE.
SET LOCAL lock_timeout = '30s';
LOCK TABLE products, price_snapshots, match_rejections IN SHARE ROW EXCLUSIVE MODE;

-- ============================== BACKUP ==============================
-- All slug rows (superset of what we touch) — for restore.
CREATE TABLE pharmonline_slug_dedup_bak_20260616 AS
SELECT p.* FROM products p
WHERE p.site = 'pharmonline' AND p.external_id !~ '^[A-Za-z0-9]{17}$';

-- ===================== TWIN MAP (slug → fresh hash twin) =====================
CREATE TEMP TABLE _twin_map AS
SELECT
    s.id AS slug_id,
    s.canonical_id AS slug_canon,
    t.twin_id,
    t.twin_canon
FROM products s
JOIN LATERAL (
    SELECT h.id AS twin_id, h.canonical_id AS twin_canon
    FROM products h
    WHERE h.site = 'pharmonline'
      AND h.external_id ~ '^[A-Za-z0-9]{17}$'
      AND h.last_seen_at >= now() - interval '2 days'
      AND h.url = s.url
    ORDER BY h.last_seen_at DESC, h.id DESC
    LIMIT 1
) t ON true
WHERE s.site = 'pharmonline' AND s.external_id !~ '^[A-Za-z0-9]{17}$';
-- (JOIN LATERAL already drops slug rows without a fresh twin → ~9783 rows)

-- ===================== SAFE-DELETE SET =====================
-- unclustered slug (slug_canon NULL), OR clustered slug whose twin is unclustered
-- (twin_canon NULL → we move membership). Excludes 305 (both clustered = conflict).
CREATE TEMP TABLE _to_delete AS
SELECT slug_id, twin_id, slug_canon, twin_canon
FROM _twin_map
WHERE slug_canon IS NULL OR twin_canon IS NULL;

-- ===================== 1. MOVE CLUSTER MEMBERSHIP =====================
-- clustered slug + unclustered twin → twin inherits the cluster, then slug dies.
UPDATE products p
SET canonical_id = d.slug_canon
FROM _to_delete d
WHERE p.id = d.twin_id
  AND p.canonical_id IS NULL  -- live re-check (not just snapshot twin_canon) vs concurrent clustering
  AND d.slug_canon IS NOT NULL
  AND d.twin_canon IS NULL;

-- ===================== SAFETY ASSERTION (aborts the whole txn on violation) =====================
-- After the membership-move, NO cluster that had a to-delete pharmonline member
-- may be left without ANY pharmonline member (the moved twin must remain). Also
-- sanity-bound the delete count. RAISE EXCEPTION → full ROLLBACK (backups vanish).
DO $$
DECLARE
    n_delete int;
    n_orphaned int;
    n_failed_move int;
BEGIN
    SELECT count(*) INTO n_delete FROM _to_delete;
    IF n_delete NOT BETWEEN 9000 AND 10200 THEN
        RAISE EXCEPTION 'dedup ABORT: delete count % out of sane range 9000-10200', n_delete;
    END IF;

    -- Every "should-move" slug's twin must NOW carry the slug's canonical_id.
    -- If a concurrent write blocked the move (live guard skipped it), this fires.
    SELECT count(*) INTO n_failed_move
    FROM _to_delete d
    WHERE d.slug_canon IS NOT NULL AND d.twin_canon IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM products p WHERE p.id = d.twin_id AND p.canonical_id = d.slug_canon
      );
    IF n_failed_move > 0 THEN
        RAISE EXCEPTION 'dedup ABORT: % membership-moves did not take (concurrent write?)', n_failed_move;
    END IF;

    -- No cluster that had a to-delete pharmonline member may end up with none.
    SELECT count(*) INTO n_orphaned
    FROM (SELECT DISTINCT slug_canon AS canon FROM _to_delete WHERE slug_canon IS NOT NULL) cc
    WHERE NOT EXISTS (
        SELECT 1 FROM products p
        WHERE p.canonical_id = cc.canon
          AND p.site = 'pharmonline'
          AND NOT EXISTS (SELECT 1 FROM _to_delete d WHERE d.slug_id = p.id)
    );
    IF n_orphaned > 0 THEN
        RAISE EXCEPTION 'dedup ABORT: % clusters would lose their pharmonline side', n_orphaned;
    END IF;
END $$;

-- ===================== 2. REPOINT match_rejections slug → twin =====================
CREATE TABLE pharmonline_slug_dedup_rej_bak_20260616 AS
SELECT mr.* FROM match_rejections mr
WHERE mr.product_a_id IN (SELECT slug_id FROM _to_delete)
   OR mr.product_b_id IN (SELECT slug_id FROM _to_delete);

-- a-side: repoint unless it would duplicate an existing (twin, b) rejection
UPDATE match_rejections mr
SET product_a_id = d.twin_id
FROM _to_delete d
WHERE mr.product_a_id = d.slug_id
  AND d.twin_id <> mr.product_b_id  -- never repoint into a self-rejection (a==b)
  AND NOT EXISTS (
      SELECT 1 FROM match_rejections x
      WHERE x.product_a_id = d.twin_id AND x.product_b_id = mr.product_b_id
  );
-- b-side: same
UPDATE match_rejections mr
SET product_b_id = d.twin_id
FROM _to_delete d
WHERE mr.product_b_id = d.slug_id
  AND d.twin_id <> mr.product_a_id  -- never repoint into a self-rejection (a==b)
  AND NOT EXISTS (
      SELECT 1 FROM match_rejections x
      WHERE x.product_b_id = d.twin_id AND x.product_a_id = mr.product_a_id
  );
-- leftover rejections still pointing at a to-delete slug row (the would-be dups)
DELETE FROM match_rejections
WHERE product_a_id IN (SELECT slug_id FROM _to_delete)
   OR product_b_id IN (SELECT slug_id FROM _to_delete);

-- ===================== 3. DELETE CHILDREN + ROWS =====================
DELETE FROM price_snapshots WHERE product_id IN (SELECT slug_id FROM _to_delete);
DELETE FROM products WHERE id IN (SELECT slug_id FROM _to_delete);

-- ===================== REPORT =====================
-- COMMIT below is safe to run unconditionally: the SAFETY ASSERTION above already
-- aborts (ROLLBACK) on any invariant violation. This report is informational.
SELECT
    (SELECT count(*) FROM _to_delete) AS deleted_rows,
    (SELECT count(*) FROM _to_delete WHERE slug_canon IS NOT NULL AND twin_canon IS NULL) AS membership_moved,
    (SELECT count(*) FROM products WHERE site = 'pharmonline') AS pharmonline_total_after,
    (SELECT count(*) FROM products WHERE site = 'pharmonline'
        AND external_id !~ '^[A-Za-z0-9]{17}$') AS slug_rows_remaining;

COMMIT;
