"""track the active MAX subscription prompt

Revision ID: 20261021_prompt_cleanup_v12
Revises: 20261021_auth_rechecks_v11
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261021_prompt_cleanup_v12"
down_revision: str | None = "20261021_auth_rechecks_v11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "max_subscription_prompts",
        sa.Column("max_user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("max_user_id"),
    )


def downgrade() -> None:
    op.drop_table("max_subscription_prompts")
