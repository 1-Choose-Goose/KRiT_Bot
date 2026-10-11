"""Audit local backup exports without storing archive data."""

import sqlalchemy as sa

from alembic import op

revision = "20261011_backup_exports_v8"
down_revision = "20261004_administration_v7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_export_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("export_id", sa.String(32), nullable=False, unique=True),
        sa.Column("installation_id", sa.String(64), nullable=False),
        sa.Column("principal_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64)),
        sa.Column("error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending','success','failed')",
            name="ck_backup_export_receipts_status",
        ),
    )
    op.create_index(
        "ix_backup_export_receipts_installation_id",
        "backup_export_receipts",
        ["installation_id"],
    )
    op.create_index(
        "ix_backup_export_receipts_principal_id",
        "backup_export_receipts",
        ["principal_id"],
    )


def downgrade() -> None:
    op.drop_table("backup_export_receipts")
