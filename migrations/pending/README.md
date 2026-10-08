# Migrations waiting for the owner's go

Alembic reads `migrations/versions/` only. A file here is finished and tested
(its tests load it by path) but is not part of the migration chain: applying
it changes what users see, so it waits for the owner's word. To release one,
`git mv` it into `migrations/versions/`, check `down_revision` against the
current head and deploy with `apply_migrations=true`.

- `0024_aloe_product_numbers.py` switches Aloe product identity from slug to
  the site's product number. See CLAUDE.md («Товар aloe узнаётся по номеру
  товара на сайте») and docs/RUNBOOK.md («Идентификаторы aloe: номер товара»).
