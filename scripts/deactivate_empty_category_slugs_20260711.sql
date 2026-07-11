-- Remove five source-confirmed empty site slugs from the active full-catalog set.
--
-- Evidence (2026-07-11): two independent production reads (Run #440 plus
-- no-persist canaries) returned zero products. Aptekonline itself reported
-- HTTP 200 + total=0 for 308/374/419/425; Pharmonline's live DDP category map
-- contained `arabalar`, but its subscription returned zero products.
--
-- Rollback (run as one transaction, then assert five restored mappings):
--   BEGIN;
--   UPDATE categories c
--   SET pharmonline_slug = b.pharmonline_slug,
--       aptekonline_slug = b.aptekonline_slug
--   FROM category_slug_deactivation_bak_20260711 b
--   WHERE c.id = b.id AND c.key = b.key
--     AND c.pharmonline_slug IS NULL AND c.aptekonline_slug IS NULL;
--   SELECT count(*) FROM categories
--   WHERE pharmonline_slug='arabalar'
--      OR aptekonline_slug IN ('308','374','419','425'); -- must be 5
--   COMMIT;

BEGIN;

DO $$
DECLARE
    matched integer;
BEGIN
    SELECT count(*) INTO matched
    FROM categories
    WHERE (key = 'pharma_arabalar' AND pharmonline_slug = 'arabalar')
       OR (key = 'aptek_308' AND aptekonline_slug = '308')
       OR (key = 'aptek_374' AND aptekonline_slug = '374')
       OR (key = 'aptek_419' AND aptekonline_slug = '419')
       OR (key = 'aptek_425' AND aptekonline_slug = '425');

    IF matched <> 5 THEN
        RAISE EXCEPTION 'expected exactly 5 guarded empty category mappings, found %', matched;
    END IF;
END $$;

-- Deliberately one-shot: a second execution fails on the backup table instead
-- of silently accepting an unknown previous migration state.
CREATE TABLE category_slug_deactivation_bak_20260711 AS
SELECT id, key, pharmonline_slug, aptekonline_slug, now() AS backed_up_at
FROM categories
WHERE key IN (
    'pharma_arabalar',
    'aptek_308',
    'aptek_374',
    'aptek_419',
    'aptek_425'
);

UPDATE categories
SET pharmonline_slug = NULL
WHERE key = 'pharma_arabalar' AND pharmonline_slug = 'arabalar';

UPDATE categories
SET aptekonline_slug = NULL
WHERE (key = 'aptek_308' AND aptekonline_slug = '308')
   OR (key = 'aptek_374' AND aptekonline_slug = '374')
   OR (key = 'aptek_419' AND aptekonline_slug = '419')
   OR (key = 'aptek_425' AND aptekonline_slug = '425');

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM categories
        WHERE pharmonline_slug = 'arabalar'
           OR aptekonline_slug IN ('308', '374', '419', '425')
    ) THEN
        RAISE EXCEPTION 'empty category mapping still active after update';
    END IF;
END $$;

COMMIT;
