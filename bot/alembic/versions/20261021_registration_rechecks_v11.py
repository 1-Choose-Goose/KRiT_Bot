"""schedule durable automatic MAX registration checks

Revision ID: 20261021_auth_rechecks_v11
Revises: 20261020_access_phone_v10
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261021_auth_rechecks_v11"
down_revision: str | None = "20261020_access_phone_v10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "max_registration_pending",
        sa.Column(
            "automatic_check_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "max_registration_pending",
        sa.Column("next_automatic_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing pending registrations have already outlived the normal schedule.
    # Give each of them one immediate final insurance check after deployment.
    op.execute(
        sa.text(
            "UPDATE max_registration_pending "
            "SET automatic_check_count = 4, next_automatic_check_at = CURRENT_TIMESTAMP "
            "WHERE status = 'pending'"
        )
    )
    op.create_index(
        "ix_max_registration_pending_automatic_check",
        "max_registration_pending",
        ["status", "next_automatic_check_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_max_registration_pending_automatic_check",
        table_name="max_registration_pending",
    )
    op.drop_column("max_registration_pending", "next_automatic_check_at")
    op.drop_column("max_registration_pending", "automatic_check_count")
