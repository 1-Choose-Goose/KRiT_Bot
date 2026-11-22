"""Link registration contracts to a customer in the people database.

Revision ID: 20261121_customer_v16
Revises: 20261121_registration_v15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261121_customer_v16"
down_revision: str | None = "20261121_registration_v15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("registration_records") as batch:
        batch.add_column(sa.Column("customer_person_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_registration_records_customer_person_id",
            "persons", ["customer_person_id"], ["id"], ondelete="SET NULL",
        )
        batch.create_index("ix_registration_records_customer_person_id", ["customer_person_id"])


def downgrade() -> None:
    raise RuntimeError("Downgrade is disabled to protect registration history")
