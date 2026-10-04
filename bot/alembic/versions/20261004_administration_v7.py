"""Administrator roles, protected bootstrap account, and token revocation state."""

import sqlalchemy as sa

from alembic import op

revision = "20261004_administration_v7"
down_revision = "20260930_communications_v6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("admin_users") as batch:
        batch.add_column(sa.Column("full_name", sa.String(250)))
        batch.add_column(
            sa.Column(
                "role",
                sa.String(24),
                nullable=False,
                server_default="superadmin",
            )
        )
        batch.add_column(
            sa.Column(
                "must_change_password",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("auth_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column(
                "is_protected",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True)))

    op.execute(sa.text("UPDATE admin_users SET full_name = username WHERE full_name IS NULL"))
    op.execute(sa.text("UPDATE admin_users SET username = lower(trim(username))"))
    op.execute(
        sa.text("UPDATE admin_users SET updated_at = created_at WHERE updated_at IS NULL")
    )

    with op.batch_alter_table("admin_users") as batch:
        batch.alter_column("full_name", existing_type=sa.String(250), nullable=False)
        batch.alter_column(
            "updated_at", existing_type=sa.DateTime(timezone=True), nullable=False
        )
        batch.create_check_constraint(
            "ck_admin_users_role",
            "role IN ('superadmin','director','administrator')",
        )


def downgrade() -> None:
    raise RuntimeError("Administrator security migration cannot be downgraded")
