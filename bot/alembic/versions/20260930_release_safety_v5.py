"""Release-safety invariants without rewriting or deleting historical records.

The migration deliberately fails when legacy data violates the new one-open-
presence invariant. An administrator must resolve those real sessions instead
of the migration inventing departure timestamps.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260930_release_safety_v5"
down_revision = "20260929_learning_operations_v4"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_indexes(table)
        if item.get("name")
    }


def _checks(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_check_constraints(table)
        if item.get("name")
    }


def _add_check(table: str, name: str, condition: str) -> None:
    if name in _checks(table):
        return
    with op.batch_alter_table(table) as batch:
        batch.create_check_constraint(name, condition)


def _add_index(table: str, name: str, columns: list[str]) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, columns)


def upgrade() -> None:
    person_columns = _columns("persons")
    if "bot_access_enabled" not in person_columns:
        with op.batch_alter_table("persons") as batch:
            batch.add_column(
                sa.Column(
                    "bot_access_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                )
            )
            batch.create_index("ix_persons_bot_access_enabled", ["bot_access_enabled"])
        op.execute(
            sa.text(
                "UPDATE persons SET bot_access_enabled = active "
                "WHERE bot_access_enabled IS NULL OR active = false"
            )
        )

    admin_columns = _columns("learning_admin_notifications")
    with op.batch_alter_table("learning_admin_notifications") as batch:
        if "condition_key" not in admin_columns:
            batch.add_column(sa.Column("condition_key", sa.String(length=180)))
            batch.create_index(
                "ix_learning_admin_notifications_condition_key", ["condition_key"]
            )
        if "resolved_at" not in admin_columns:
            batch.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True)))
            batch.create_index(
                "ix_learning_admin_notifications_resolved_at", ["resolved_at"]
            )
        unique_names = {
            str(item["name"])
            for item in sa.inspect(op.get_bind()).get_unique_constraints(
                "learning_admin_notifications"
            )
            if item.get("name")
        }
        if "uq_admin_notification_lesson_kind" in unique_names:
            batch.drop_constraint("uq_admin_notification_lesson_kind", type_="unique")

    lesson_columns = _columns("learning_lessons")
    with op.batch_alter_table("learning_lessons") as batch:
        if "series_occurrence_index" not in lesson_columns:
            batch.add_column(sa.Column("series_occurrence_index", sa.Integer()))
        if "series_exception" not in lesson_columns:
            batch.add_column(
                sa.Column(
                    "series_exception",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
    op.execute(
        sa.text(
            "WITH ranked AS ("
            "SELECT id, ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY start_at, id) - 1 "
            "AS occurrence_index FROM learning_lessons WHERE series_id IS NOT NULL"
            ") UPDATE learning_lessons SET series_occurrence_index = ("
            "SELECT occurrence_index FROM ranked WHERE ranked.id = learning_lessons.id"
            ") WHERE series_id IS NOT NULL AND series_occurrence_index IS NULL"
        )
    )
    lesson_unique_names = {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_unique_constraints("learning_lessons")
        if item.get("name")
    }
    if "uq_lesson_series_occurrence" not in lesson_unique_names:
        with op.batch_alter_table("learning_lessons") as batch:
            batch.create_unique_constraint(
                "uq_lesson_series_occurrence",
                ["series_id", "series_occurrence_index"],
            )

    notification_columns = _columns("learning_notification_jobs")
    with op.batch_alter_table("learning_notification_jobs") as batch:
        if "last_attempt_at" not in notification_columns:
            batch.add_column(sa.Column("last_attempt_at", sa.DateTime(timezone=True)))
        if "external_message_id" not in notification_columns:
            batch.add_column(sa.Column("external_message_id", sa.String(length=180)))

    if "uq_admin_notification_open_condition" not in _indexes(
        "learning_admin_notifications"
    ):
        predicate = sa.text("condition_key IS NOT NULL AND resolved_at IS NULL")
        op.create_index(
            "uq_admin_notification_open_condition",
            "learning_admin_notifications",
            ["condition_key"],
            unique=True,
            sqlite_where=predicate,
            postgresql_where=predicate,
        )

    duplicate_open_presence = op.get_bind().execute(
        sa.text(
            "SELECT person_id FROM learning_club_presence_sessions "
            "WHERE left_at IS NULL GROUP BY person_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_open_presence is not None:
        raise RuntimeError(
            "Найдены несколько незакрытых посещений одного человека. "
            "Исправьте их через журнал до повторного запуска krit-migrate."
        )
    if "uq_presence_one_open_per_person" not in _indexes(
        "learning_club_presence_sessions"
    ):
        predicate = sa.text("left_at IS NULL")
        op.create_index(
            "uq_presence_one_open_per_person",
            "learning_club_presence_sessions",
            ["person_id"],
            unique=True,
            sqlite_where=predicate,
            postgresql_where=predicate,
        )

    checks = (
        (
            "syndication_jobs",
            "ck_syndication_job_status",
            "status IN ('pending','processing','published','retry','failed','skipped')",
        ),
        (
            "learning_group_memberships",
            "ck_group_membership_period",
            "end_at IS NULL OR end_at > start_at",
        ),
        ("learning_lesson_series", "ck_series_interval_positive", "interval_weeks > 0"),
        ("learning_lessons", "ck_lesson_period", "end_at > start_at"),
        (
            "learning_lessons",
            "ck_lesson_status",
            "status IN ('planned','scheduled','in_progress','completed','cancelled')",
        ),
        (
            "learning_lesson_participants",
            "ck_lesson_attendance_status",
            "attendance_status IN "
            "('expected','present','late','absent','left_early','excused')",
        ),
        (
            "learning_lesson_participants",
            "ck_lesson_participant_cancelled_by",
            "cancelled_by IS NULL OR cancelled_by IN "
            "('student','guardian','administrator')",
        ),
        (
            "learning_notification_jobs",
            "ck_learning_notification_status",
            "status IN ('pending','processing','sent','retry','failed','cancelled')",
        ),
        (
            "learning_club_presence_sessions",
            "ck_presence_period",
            "left_at IS NULL OR left_at >= arrived_at",
        ),
    )
    for table, name, condition in checks:
        _add_check(table, name, condition)

    indexes = (
        ("persons", "ix_persons_active", ["active"]),
        ("persons", "ix_persons_archived_at", ["archived_at"]),
        ("persons", "ix_persons_max_user_id", ["max_user_id"]),
        ("persons", "ix_persons_phone", ["phone"]),
        ("person_roles", "ix_person_roles_role", ["role"]),
        ("admin_users", "ix_admin_users_username", ["username"]),
        ("syndication_jobs", "ix_syndication_jobs_integration_id", ["integration_id"]),
        ("syndication_jobs", "ix_syndication_jobs_next_attempt_at", ["next_attempt_at"]),
        ("syndication_jobs", "ix_syndication_jobs_status", ["status"]),
        ("syndication_jobs", "ix_syndication_jobs_vk_event_id", ["vk_event_id"]),
        (
            "person_max_identities",
            "ix_person_max_identities_max_user_id",
            ["max_user_id"],
        ),
        ("learning_subjects", "ix_learning_subjects_active", ["active"]),
        ("learning_rooms", "ix_learning_rooms_active", ["active"]),
        ("learning_groups", "ix_learning_groups_active", ["active"]),
        ("learning_groups", "ix_learning_groups_subject_id", ["subject_id"]),
        (
            "learning_group_memberships",
            "ix_learning_group_memberships_group_id",
            ["group_id"],
        ),
        (
            "learning_group_memberships",
            "ix_learning_group_memberships_person_id",
            ["person_id"],
        ),
        ("learning_lesson_series", "ix_learning_lesson_series_group_id", ["group_id"]),
        ("learning_lesson_series", "ix_learning_lesson_series_room_id", ["room_id"]),
        (
            "learning_lesson_series",
            "ix_learning_lesson_series_subject_id",
            ["subject_id"],
        ),
        (
            "learning_lesson_series",
            "ix_learning_lesson_series_teacher_id",
            ["teacher_id"],
        ),
        ("learning_lessons", "ix_learning_lessons_group_id", ["group_id"]),
        ("learning_lessons", "ix_learning_lessons_room_id", ["room_id"]),
        ("learning_lessons", "ix_learning_lessons_series_id", ["series_id"]),
        ("learning_lessons", "ix_learning_lessons_subject_id", ["subject_id"]),
        ("learning_lessons", "ix_learning_lessons_teacher_id", ["teacher_id"]),
        (
            "learning_lesson_participants",
            "ix_learning_lesson_participants_cancelled_by_admin_id",
            ["cancelled_by_admin_id"],
        ),
        (
            "learning_lesson_participants",
            "ix_learning_lesson_participants_cancelled_by_person_id",
            ["cancelled_by_person_id"],
        ),
        (
            "learning_lesson_participants",
            "ix_learning_lesson_participants_lesson_id",
            ["lesson_id"],
        ),
        (
            "learning_lesson_participants",
            "ix_learning_lesson_participants_person_id",
            ["person_id"],
        ),
        (
            "learning_lesson_teacher_segments",
            "ix_learning_lesson_teacher_segments_lesson_id",
            ["lesson_id"],
        ),
        (
            "learning_lesson_teacher_segments",
            "ix_learning_lesson_teacher_segments_teacher_person_id",
            ["teacher_person_id"],
        ),
        (
            "learning_club_presence_sessions",
            "ix_learning_club_presence_sessions_person_id",
            ["person_id"],
        ),
        (
            "learning_notification_jobs",
            "ix_learning_notification_jobs_lesson_id",
            ["lesson_id"],
        ),
        (
            "learning_notification_jobs",
            "ix_learning_notification_jobs_recipient_person_id",
            ["recipient_person_id"],
        ),
        (
            "learning_admin_notifications",
            "ix_learning_admin_notifications_kind",
            ["kind"],
        ),
        (
            "learning_admin_notifications",
            "ix_learning_admin_notifications_lesson_id",
            ["lesson_id"],
        ),
        (
            "learning_admin_notifications",
            "ix_learning_admin_notifications_read_at",
            ["read_at"],
        ),
        ("learning_audit_events", "ix_learning_audit_events_action", ["action"]),
        (
            "learning_audit_events",
            "ix_learning_audit_events_actor_admin_id",
            ["actor_admin_id"],
        ),
        (
            "learning_audit_events",
            "ix_learning_audit_events_entity_id",
            ["entity_id"],
        ),
        ("learning_lessons", "ix_lesson_room_period", ["room_id", "start_at", "end_at"]),
        (
            "learning_lessons",
            "ix_lesson_teacher_period",
            ["teacher_id", "start_at", "end_at"],
        ),
        ("learning_lessons", "ix_lesson_status_start", ["status", "start_at"]),
        (
            "learning_lesson_participants",
            "ix_participant_person_lesson",
            ["person_id", "lesson_id"],
        ),
        (
            "learning_group_memberships",
            "ix_group_membership_active",
            ["group_id", "person_id", "end_at"],
        ),
        (
            "learning_notification_jobs",
            "ix_learning_notification_due",
            ["status", "scheduled_at"],
        ),
        (
            "learning_club_presence_sessions",
            "ix_presence_person_open",
            ["person_id", "left_at"],
        ),
        (
            "learning_lesson_teacher_segments",
            "ix_teacher_segment_lesson_period",
            ["lesson_id", "started_at"],
        ),
        (
            "learning_lesson_teacher_segments",
            "ix_teacher_segment_teacher_period",
            ["teacher_person_id", "started_at"],
        ),
    )
    for table, name, columns in indexes:
        _add_index(table, name, columns)


def downgrade() -> None:
    raise RuntimeError("Downgrade is disabled to protect production history")
