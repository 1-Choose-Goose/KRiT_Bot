"""Add correspondence and contract registration journals.

Revision ID: 20261121_registration_v15
Revises: 20261025_attendance_v14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261121_registration_v15"
down_revision: str | None = "20261025_attendance_v14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "registration_counters",
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("next_value", sa.Integer(), nullable=False),
    )
    op.create_table(
        "registration_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("registration_number", sa.String(32), nullable=False, unique=True),
        sa.Column("registration_date", sa.Date(), nullable=False),
        sa.Column("document_date", sa.Date()),
        sa.Column("document_number", sa.String(100)),
        sa.Column("counterparty", sa.String(250), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("persons.id", ondelete="SET NULL")),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("kind", "year", "sequence"),
    )
    op.create_index("ix_registration_records_kind", "registration_records", ["kind"])


def downgrade() -> None:
    raise RuntimeError("Downgrade is disabled to protect registration history")
