"""Operational lesson history, notification dedupe and MAX auth phone."""

import sqlalchemy as sa

from alembic import op

revision = "20260929_learning_operations_v4"
down_revision = "20260929_subject_teachers_v3"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    person_columns = _columns("persons")
    phone_constraints = [
        item.get("name")
        for item in sa.inspect(op.get_bind()).get_unique_constraints("persons")
        if item.get("column_names") == ["phone"] and item.get("name")
    ]
    with op.batch_alter_table("persons") as batch:
        for constraint_name in phone_constraints:
            batch.drop_constraint(str(constraint_name), type_="unique")
        if "max_auth_phone" not in person_columns:
            batch.add_column(sa.Column("max_auth_phone", sa.String(length=32)))
            batch.create_index("ix_persons_max_auth_phone", ["max_auth_phone"])
    op.execute(
        sa.text(
            "UPDATE persons SET max_auth_phone = phone "
            "WHERE max_auth_phone IS NULL AND max_user_id IS NOT NULL"
        )
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_persons_active_max_auth_phone",
            "persons",
            ["max_auth_phone"],
            unique=True,
            postgresql_where=sa.text(
                "max_auth_phone IS NOT NULL AND active IS TRUE AND archived_at IS NULL"
            ),
        )
    else:
        op.create_index(
            "uq_persons_active_max_auth_phone",
            "persons",
            ["max_auth_phone"],
            unique=True,
            sqlite_where=sa.text(
                "max_auth_phone IS NOT NULL AND active = 1 AND archived_at IS NULL"
            ),
        )

    lesson_columns = _columns("learning_lessons")
    with op.batch_alter_table("learning_lessons") as batch:
        if "completion_type" not in lesson_columns:
            batch.add_column(sa.Column("completion_type", sa.String(length=16)))
        if "completion_reason" not in lesson_columns:
            batch.add_column(sa.Column("completion_reason", sa.String(length=500)))
        if "completion_public_comment" not in lesson_columns:
            batch.add_column(sa.Column("completion_public_comment", sa.String(length=500)))

    participant_columns = _columns("learning_lesson_participants")
    if "early_leave_reason" not in participant_columns:
        with op.batch_alter_table("learning_lesson_participants") as batch:
            batch.add_column(sa.Column("early_leave_reason", sa.String(length=500)))

    if "learning_lesson_teacher_segments" not in _tables():
        op.create_table(
            "learning_lesson_teacher_segments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "lesson_id",
                sa.Integer(),
                sa.ForeignKey("learning_lessons.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "teacher_person_id",
                sa.Integer(),
                sa.ForeignKey("persons.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("teacher_name_snapshot", sa.String(length=250), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ended_at", sa.DateTime(timezone=True)),
            sa.Column("segment_type", sa.String(length=16), nullable=False),
            sa.Column("reason", sa.String(length=500)),
            sa.Column(
                "created_by_admin_id",
                sa.Integer(),
                sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.CheckConstraint(
                "ended_at IS NULL OR ended_at >= started_at",
                name="ck_teacher_segment_period",
            ),
            sa.CheckConstraint(
                "segment_type IN ('primary','substitute')",
                name="ck_teacher_segment_type",
            ),
        )
        op.create_index(
            "ix_teacher_segment_lesson_start",
            "learning_lesson_teacher_segments",
            ["lesson_id", "started_at"],
        )
        op.create_index(
            "ix_teacher_segment_teacher_start",
            "learning_lesson_teacher_segments",
            ["teacher_person_id", "started_at"],
        )
        op.execute(
            sa.text(
                """
                INSERT INTO learning_lesson_teacher_segments
                    (lesson_id, teacher_person_id, teacher_name_snapshot, started_at,
                     ended_at, segment_type, created_at)
                SELECT id, teacher_id, teacher_name_snapshot,
                       COALESCE(actual_start_at, start_at),
                       CASE WHEN status = 'completed' THEN COALESCE(actual_end_at, end_at)
                            ELSE NULL END,
                       'primary', CURRENT_TIMESTAMP
                FROM learning_lessons
                WHERE status IN ('in_progress', 'completed')
                """
            )
        )

    admin_columns = _columns("learning_admin_notifications")
    if "dedupe_key" not in admin_columns:
        with op.batch_alter_table("learning_admin_notifications") as batch:
            batch.add_column(sa.Column("dedupe_key", sa.String(length=180)))
        op.execute(
            sa.text(
                "UPDATE learning_admin_notifications "
                "SET dedupe_key = 'legacy:' || id WHERE dedupe_key IS NULL"
            )
        )
        with op.batch_alter_table("learning_admin_notifications") as batch:
            batch.alter_column("dedupe_key", existing_type=sa.String(180), nullable=False)
            batch.create_unique_constraint("uq_admin_notification_dedupe", ["dedupe_key"])
    op.execute(
        sa.text(
            "DELETE FROM learning_admin_notifications "
            "WHERE lesson_id IS NOT NULL AND id NOT IN ("
            "SELECT MIN(id) FROM learning_admin_notifications "
            "WHERE lesson_id IS NOT NULL GROUP BY lesson_id, kind)"
        )
    )
    notification_constraints = {
        item.get("name")
        for item in sa.inspect(op.get_bind()).get_unique_constraints(
            "learning_admin_notifications"
        )
    }
    if "uq_admin_notification_lesson_kind" not in notification_constraints:
        with op.batch_alter_table("learning_admin_notifications") as batch:
            batch.create_unique_constraint(
                "uq_admin_notification_lesson_kind", ["lesson_id", "kind"]
            )


def downgrade() -> None:
    raise RuntimeError("Downgrade is disabled to protect learning history")
