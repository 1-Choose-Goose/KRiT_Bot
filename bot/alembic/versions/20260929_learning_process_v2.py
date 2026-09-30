"""Group defaults and participant cancellation provenance."""

import sqlalchemy as sa

from alembic import op

revision = "20260929_learning_process_v2"
down_revision = "20260928_learning_process_v1"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _check_constraints(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_check_constraints(table)
        if item.get("name")
    }


def upgrade() -> None:
    group_columns = _columns("learning_groups")
    with op.batch_alter_table("learning_groups") as batch:
        if "default_teacher_id" not in group_columns:
            batch.add_column(sa.Column("default_teacher_id", sa.Integer()))
            batch.create_foreign_key(
                "fk_learning_groups_default_teacher",
                "persons",
                ["default_teacher_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_index("ix_learning_groups_default_teacher_id", ["default_teacher_id"])
        if "default_duration_minutes" not in group_columns:
            batch.add_column(
                sa.Column(
                    "default_duration_minutes",
                    sa.Integer(),
                    nullable=False,
                    server_default="60",
                )
            )

    participant_columns = _columns("learning_lesson_participants")
    additions = [
        ("cancelled_at", sa.DateTime(timezone=True)),
        ("cancelled_by", sa.String(length=20)),
        ("cancelled_by_person_id", sa.Integer()),
        ("cancelled_by_admin_id", sa.Integer()),
        ("cancellation_reason", sa.String(length=500)),
    ]
    with op.batch_alter_table("learning_lesson_participants") as batch:
        for name, column_type in additions:
            if name not in participant_columns:
                batch.add_column(sa.Column(name, column_type, nullable=True))
        if "cancelled_by_person_id" not in participant_columns:
            batch.create_foreign_key(
                "fk_lesson_participant_cancel_person",
                "persons",
                ["cancelled_by_person_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_index(
                "ix_lesson_participants_cancelled_by_person_id",
                ["cancelled_by_person_id"],
            )
        if "cancelled_by_admin_id" not in participant_columns:
            batch.create_foreign_key(
                "fk_lesson_participant_cancel_admin",
                "admin_users",
                ["cancelled_by_admin_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_index(
                "ix_lesson_participants_cancelled_by_admin_id",
                ["cancelled_by_admin_id"],
            )
    if (
        op.get_bind().dialect.name == "postgresql"
        and "ck_lesson_participant_cancelled_by"
        not in _check_constraints("learning_lesson_participants")
    ):
        op.create_check_constraint(
            "ck_lesson_participant_cancelled_by",
            "learning_lesson_participants",
            "cancelled_by IS NULL OR cancelled_by IN ('student','guardian','administrator')",
        )

    # Adopt every legacy MAX binding before runtime switches exclusively to
    # PersonMaxIdentity. ON CONFLICT protects databases where part of the data
    # was already copied by the transitional startup routine.
    op.execute(
        sa.text(
            """
            INSERT INTO person_max_identities
                (person_id, verified_phone, max_user_id, verified_at, updated_at)
            SELECT id, phone, max_user_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM persons
            WHERE max_user_id IS NOT NULL
            ON CONFLICT DO NOTHING
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError("Downgrade is disabled to protect production learning history")
