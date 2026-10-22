"""add explicit online lessons

Revision ID: 20261021_online_lessons_v13
Revises: 20261021_prompt_cleanup_v12
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261021_online_lessons_v13"
down_revision: str | None = "20261021_prompt_cleanup_v12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("learning_lesson_series") as batch:
        batch.add_column(
            sa.Column(
                "delivery_mode",
                sa.String(length=16),
                nullable=False,
                server_default="offline",
            )
        )
        batch.alter_column("room_id", existing_type=sa.Integer(), nullable=True)
        batch.create_check_constraint(
            "ck_series_delivery_mode", "delivery_mode IN ('offline','online')"
        )

    with op.batch_alter_table("learning_lessons") as batch:
        batch.add_column(
            sa.Column(
                "delivery_mode",
                sa.String(length=16),
                nullable=False,
                server_default="offline",
            )
        )
        batch.alter_column("room_id", existing_type=sa.Integer(), nullable=True)
        batch.create_check_constraint(
            "ck_lesson_delivery_mode", "delivery_mode IN ('offline','online')"
        )


def downgrade() -> None:
    raise RuntimeError("Downgrade is disabled to protect production learning history")
