"""roi_actions_cache table

Revision ID: 0005_roi_cache
Revises: 0003_notif_prefs
Create Date: 2026-05-17

History note (2026-05-28): originally `Revises: 0004_norm_attrs` (AI normalizer
+ structured-attrs matcher migration, commit 33c8495). That migration was later
reverted/deleted but down_revision не обновился — ломало fresh deploys (CI
alembic upgrade head failed `KeyError: 0004_norm_attrs`). Прод-DB уже на 0008
head, не impacted. Fixed: down_revision now points back to 0003 (skipping
deleted 0004).

ROI actions endpoint (`compute_actions`) делает 4 функции с N+1 SQL по 3388
матчам — 15-30с. Frontend timeout'ит на 15с и показывает `API 408` на 4
страницах из 11. Pre-compute после scrape, cache в эту таблицу, serve из
неё → <50мс HTTP latency.

Структура:
- client_site UNIQUE — для PK на (tenant_id, client_site)
- payload JSON — сериализованный list[ActionItem]
- computed_at — для определения staleness, fallback recompute если > 26h
- run_id — какой run сгенерил кэш (для отладки)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_roi_cache"
down_revision: str | None = "0003_notif_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(bind, table: str) -> bool:
    return table in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if _table_exists(bind, "roi_actions_cache"):
        return

    op.create_table(
        "roi_actions_cache",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, nullable=False, server_default="1"),
        sa.Column("client_site", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("run_id", sa.Integer, nullable=True),
        sa.UniqueConstraint("tenant_id", "client_site", name="uq_roi_cache_tenant_site"),
    )
    op.create_index(
        "idx_roi_cache_computed_at",
        "roi_actions_cache",
        ["computed_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "roi_actions_cache"):
        return
    op.drop_index("idx_roi_cache_computed_at", table_name="roi_actions_cache")
    op.drop_table("roi_actions_cache")
