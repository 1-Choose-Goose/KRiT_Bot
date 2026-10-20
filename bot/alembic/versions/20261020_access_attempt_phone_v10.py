"""store verified phones for MAX authorization attempts

Revision ID: 20261020_access_phone_v10
Revises: 20261018_online_learning_v9
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261020_access_phone_v10"
down_revision: str | None = "20261018_online_learning_v9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "access_attempts",
        sa.Column("verified_phone", sa.String(length=32), nullable=True),
    )
    op.create_index(
        op.f("ix_access_attempts_verified_phone"),
        "access_attempts",
        ["verified_phone"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_access_attempts_verified_phone"), table_name="access_attempts")
    op.drop_column("access_attempts", "verified_phone")
