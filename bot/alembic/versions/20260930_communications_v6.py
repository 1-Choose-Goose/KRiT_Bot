"""Universal communications, policies, daily bundles, interactions and history."""

from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "20260930_communications_v6"
down_revision = "20260930_release_safety_v5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_global_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_code", sa.String(64), nullable=False),
        sa.Column("recipient_context", sa.String(16), nullable=False, server_default="*"),
        sa.Column("offset_minutes", sa.Integer(), nullable=False, server_default="-1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("requires_confirmation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("priority", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("quiet_hours_policy", sa.String(16), nullable=False, server_default="defer"),
        sa.Column("quiet_start", sa.String(5)),
        sa.Column("quiet_end", sa.String(5)),
        sa.Column("configuration", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("recipient_context IN ('*','student','guardian','teacher')"),
        sa.CheckConstraint("priority IN ('low','normal','high')"),
        sa.UniqueConstraint(
            "event_code", "recipient_context", "offset_minutes", name="uq_global_rule_key"
        ),
    )
    op.create_index(
        "ix_notification_global_rules_event_code", "notification_global_rules", ["event_code"]
    )
    op.create_table(
        "person_notification_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("recipient_context", sa.String(16), nullable=False),
        sa.Column("event_code", sa.String(64), nullable=False),
        sa.Column("offset_minutes", sa.Integer(), nullable=False, server_default="-1"),
        sa.Column("state", sa.String(12), nullable=False, server_default="inherit"),
        sa.Column("configuration", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('inherit','on','off')"),
        sa.CheckConstraint("recipient_context IN ('student','guardian','teacher')"),
        sa.UniqueConstraint(
            "person_id",
            "recipient_context",
            "event_code",
            "offset_minutes",
            name="uq_person_notification_override",
        ),
    )
    op.create_index(
        "ix_person_notification_overrides_person_id", "person_notification_overrides", ["person_id"]
    )
    op.create_table(
        "guardian_notification_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "guardian_person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "student_person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_code", sa.String(64), nullable=False),
        sa.Column("offset_minutes", sa.Integer(), nullable=False, server_default="-1"),
        sa.Column("state", sa.String(12), nullable=False, server_default="inherit"),
        sa.Column("configuration", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('inherit','on','off')"),
        sa.UniqueConstraint(
            "guardian_person_id",
            "student_person_id",
            "event_code",
            "offset_minutes",
            name="uq_guardian_notification_override",
        ),
    )
    op.create_index(
        "ix_guardian_notification_overrides_guardian_person_id",
        "guardian_notification_overrides",
        ["guardian_person_id"],
    )
    op.create_index(
        "ix_guardian_notification_overrides_student_person_id",
        "guardian_notification_overrides",
        ["student_person_id"],
    )
    op.create_table(
        "communication_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_by_admin_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
        ),
        sa.Column("campaign_type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(250), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "campaign_type IN ('manual_message','custom_poll',"
            "'schedule_publication','schedule_change')"
        ),
        sa.CheckConstraint(
            "status IN ('draft','scheduled','sending','completed','partial','failed')"
        ),
    )
    op.create_index(
        "ix_communication_campaigns_created_by_admin_id",
        "communication_campaigns",
        ["created_by_admin_id"],
    )
    op.create_index(
        "ix_communication_campaigns_campaign_type", "communication_campaigns", ["campaign_type"]
    )
    op.create_index(
        "ix_communication_campaigns_scheduled_at", "communication_campaigns", ["scheduled_at"]
    )
    op.create_index("ix_communication_campaigns_status", "communication_campaigns", ["status"])
    op.create_table(
        "communication_threads",
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("last_message_preview", sa.String(240)),
        sa.Column("admin_unread_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("admin_read_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_communication_threads_last_message_at", "communication_threads", ["last_message_at"]
    )
    op.create_table(
        "interaction_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_type", sa.String(32), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column(
            "recipient_person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("recipient_context", sa.String(16), nullable=False),
        sa.Column(
            "subject_person_id", sa.Integer(), sa.ForeignKey("persons.id", ondelete="CASCADE")
        ),
        sa.Column(
            "related_lesson_id",
            sa.Integer(),
            sa.ForeignKey("learning_lessons.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("communication_campaigns.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_by_admin_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
        ),
        sa.Column("request_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "expects_reason_from_person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="SET NULL"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("request_type IN ('yes_no','lesson_confirmation','custom')"),
        sa.CheckConstraint("status IN ('draft','active','answered','expired','cancelled')"),
    )
    for name, columns in (
        ("ix_interaction_requests_request_type", ["request_type"]),
        ("ix_interaction_requests_recipient_person_id", ["recipient_person_id"]),
        ("ix_interaction_requests_subject_person_id", ["subject_person_id"]),
        ("ix_interaction_requests_related_lesson_id", ["related_lesson_id"]),
        ("ix_interaction_requests_campaign_id", ["campaign_id"]),
        ("ix_interaction_requests_status", ["status"]),
        ("ix_interaction_requests_expires_at", ["expires_at"]),
        (
            "ix_interaction_requests_expects_reason_from_person_id",
            ["expects_reason_from_person_id"],
        ),
        ("ix_interaction_request_expiry", ["status", "expires_at"]),
    ):
        op.create_index(name, "interaction_requests", columns)
    op.create_table(
        "interaction_request_lessons",
        sa.Column(
            "request_id",
            sa.Integer(),
            sa.ForeignKey("interaction_requests.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "lesson_id",
            sa.Integer(),
            sa.ForeignKey("learning_lessons.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("lesson_revision", sa.Integer(), nullable=False),
    )
    op.create_table(
        "interaction_responses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Integer(),
            sa.ForeignKey("interaction_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "respondent_person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("respondent_context", sa.String(16), nullable=False),
        sa.Column("answer", sa.String(16), nullable=False),
        sa.Column("lesson_answers", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reason", sa.Text()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("answer IN ('yes','no','partial')"),
        sa.UniqueConstraint(
            "request_id",
            "respondent_person_id",
            "respondent_context",
            name="uq_interaction_current_response",
        ),
    )
    op.create_index("ix_interaction_responses_request_id", "interaction_responses", ["request_id"])
    op.create_index(
        "ix_interaction_responses_respondent_person_id",
        "interaction_responses",
        ["respondent_person_id"],
    )
    op.create_index(
        "ix_interaction_response_request_person",
        "interaction_responses",
        ["request_id", "respondent_person_id"],
    )
    op.create_table(
        "interaction_response_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "response_id",
            sa.Integer(),
            sa.ForeignKey("interaction_responses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("old_answer", sa.String(16)),
        sa.Column("new_answer", sa.String(16), nullable=False),
        sa.Column("old_lesson_answers", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("new_lesson_answers", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_interaction_response_history_response_id",
        "interaction_response_history",
        ["response_id"],
    )
    op.create_table(
        "lesson_attendance_intents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "lesson_id",
            sa.Integer(),
            sa.ForeignKey("learning_lessons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "student_person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lesson_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column(
            "last_request_id",
            sa.Integer(),
            sa.ForeignKey("interaction_requests.id", ondelete="SET NULL"),
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','confirmed','declined','no_response',"
            "'needs_reconfirmation','conflict')"
        ),
        sa.UniqueConstraint(
            "lesson_id", "student_person_id", "lesson_revision", name="uq_lesson_intent_revision"
        ),
    )
    op.create_index(
        "ix_lesson_attendance_intents_lesson_id", "lesson_attendance_intents", ["lesson_id"]
    )
    op.create_index(
        "ix_lesson_attendance_intents_student_person_id",
        "lesson_attendance_intents",
        ["student_person_id"],
    )
    op.create_index("ix_lesson_attendance_intents_status", "lesson_attendance_intents", ["status"])
    op.create_index(
        "ix_lesson_intent_lesson_student",
        "lesson_attendance_intents",
        ["lesson_id", "student_person_id"],
    )
    op.create_table(
        "schedule_publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("period_from", sa.Date(), nullable=False),
        sa.Column("period_to", sa.Date(), nullable=False),
        sa.Column(
            "created_by_admin_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("communication_campaigns.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "schedule_recipient_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "publication_id",
            sa.Integer(),
            sa.ForeignKey("schedule_publications.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "recipient_person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("recipient_context", sa.String(16), nullable=False),
        sa.Column(
            "subject_person_id",
            sa.Integer(),
            sa.ForeignKey(
                "persons.id",
                ondelete="CASCADE",
                name="fk_notification_jobs_subject_person",
            ),
            nullable=False,
        ),
        sa.Column(
            "lesson_id",
            sa.Integer(),
            sa.ForeignKey("learning_lessons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lesson_revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "recipient_person_id",
            "recipient_context",
            "subject_person_id",
            "lesson_id",
            name="uq_schedule_recipient_lesson",
        ),
    )
    op.create_index(
        "ix_schedule_recipient_snapshots_publication_id",
        "schedule_recipient_snapshots",
        ["publication_id"],
    )
    op.create_index(
        "ix_schedule_recipient_snapshots_recipient_person_id",
        "schedule_recipient_snapshots",
        ["recipient_person_id"],
    )
    op.create_index(
        "ix_schedule_recipient_snapshots_subject_person_id",
        "schedule_recipient_snapshots",
        ["subject_person_id"],
    )
    op.create_index(
        "ix_schedule_recipient_snapshots_lesson_id", "schedule_recipient_snapshots", ["lesson_id"]
    )
    op.create_index(
        "ix_schedule_snapshot_recipient_lesson",
        "schedule_recipient_snapshots",
        ["recipient_person_id", "lesson_id"],
    )
    op.create_table(
        "max_registration_pending",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("max_user_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("verified_phone", sa.String(32), nullable=False),
        sa.Column("required_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_max_registration_pending_person_id", "max_registration_pending", ["person_id"]
    )
    op.create_index(
        "ix_max_registration_pending_expiry", "max_registration_pending", ["expires_at"]
    )

    with op.batch_alter_table("learning_lessons") as batch:
        batch.add_column(
            sa.Column("notification_revision", sa.Integer(), nullable=False, server_default="1")
        )
    with op.batch_alter_table("person_max_identities") as batch:
        batch.add_column(
            sa.Column(
                "channel_subscription_status",
                sa.String(16),
                nullable=False,
                server_default="unknown",
            )
        )
        batch.add_column(sa.Column("channel_subscription_checked_at", sa.DateTime(timezone=True)))
    with op.batch_alter_table("learning_notification_jobs") as batch:
        batch.add_column(
            sa.Column("recipient_context", sa.String(16), nullable=False, server_default="student")
        )
        batch.add_column(
            sa.Column(
                "subject_person_id",
                sa.Integer(),
                sa.ForeignKey(
                    "persons.id",
                    ondelete="CASCADE",
                    name="fk_notification_jobs_subject_person",
                ),
            )
        )
        batch.add_column(sa.Column("priority", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(
            sa.Column(
                "campaign_id",
                sa.Integer(),
                sa.ForeignKey(
                    "communication_campaigns.id",
                    ondelete="SET NULL",
                    name="fk_notification_jobs_campaign",
                ),
            )
        )
        batch.add_column(
            sa.Column(
                "interaction_request_id",
                sa.Integer(),
                sa.ForeignKey(
                    "interaction_requests.id",
                    ondelete="SET NULL",
                    name="fk_notification_jobs_interaction_request",
                ),
            )
        )
        batch.create_index("ix_learning_notification_recipient", ["recipient_person_id"])
        batch.create_index("ix_learning_notification_jobs_subject_person_id", ["subject_person_id"])
        batch.create_index("ix_learning_notification_jobs_campaign_id", ["campaign_id"])
        batch.create_index(
            "ix_learning_notification_jobs_interaction_request_id", ["interaction_request_id"]
        )
    # Legacy per-lesson reminders would race the new daily bundle reconciler.
    # Keep sent history intact, cancel only unsent legacy jobs, then let the
    # maintenance task rebuild future reminders with deterministic bundle keys.
    op.get_bind().execute(
        sa.text(
            "UPDATE learning_notification_jobs "
            "SET status = 'cancelled', "
            "last_error = 'Rebuilt as a daily communication bundle' "
            "WHERE status IN ('pending', 'retry') "
            "AND event_type LIKE 'lesson_reminder_%'"
        )
    )

    op.create_table(
        "communication_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("persons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("message_type", sa.String(32), nullable=False, server_default="text"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("max_message_id", sa.String(180)),
        sa.Column("delivery_status", sa.String(16), nullable=False),
        sa.Column(
            "related_lesson_id",
            sa.Integer(),
            sa.ForeignKey("learning_lessons.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "interaction_request_id",
            sa.Integer(),
            sa.ForeignKey("interaction_requests.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("communication_campaigns.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "outbox_job_id",
            sa.Integer(),
            sa.ForeignKey("learning_notification_jobs.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("direction IN ('inbound','outbound')"),
        sa.CheckConstraint(
            "delivery_status IN ('received','pending','sending','sent','failed','unavailable')"
        ),
    )
    for name, columns in (
        ("ix_communication_messages_person_id", ["person_id"]),
        ("ix_communication_messages_max_message_id", ["max_message_id"]),
        ("ix_communication_messages_related_lesson_id", ["related_lesson_id"]),
        ("ix_communication_messages_interaction_request_id", ["interaction_request_id"]),
        ("ix_communication_messages_campaign_id", ["campaign_id"]),
        ("ix_communication_messages_created_at", ["created_at"]),
        ("ix_communication_message_person_created", ["person_id", "created_at"]),
    ):
        op.create_index(name, "communication_messages", columns)

    bind = op.get_bind()
    # Executemany needs concrete values.  SQL expressions such as func.now()
    # cannot be bound as parameters by asyncpg.
    now = datetime.now(UTC)
    rules = []
    for context in ("student", "guardian", "teacher"):
        for offset in (1440, 180, 60):
            rules.append(
                {
                    "event_code": "lesson_reminder",
                    "recipient_context": context,
                    "offset_minutes": offset,
                    "enabled": True,
                    "requires_confirmation": False,
                    "priority": "normal",
                    "quiet_hours_policy": "defer",
                    "quiet_start": "22:00",
                    "quiet_end": "08:00",
                    "configuration": {"after_confirmation": "one_hour_only"},
                    "updated_at": now,
                }
            )
        if context != "teacher":
            for offset in (1440, 180, 60):
                rules.append(
                    {
                        "event_code": "lesson_confirmation_request",
                        "recipient_context": context,
                        "offset_minutes": offset,
                        "enabled": offset == 1440,
                        "requires_confirmation": True,
                        "priority": "normal",
                        "quiet_hours_policy": "defer",
                        "quiet_start": "22:00",
                        "quiet_end": "08:00",
                        "configuration": {
                            "deadline_minutes": 60,
                            "follow_up": "once",
                            "follow_up_offset_minutes": 180,
                        },
                        "updated_at": now,
                    }
                )
    for event_code in (
        "schedule_published",
        "schedule_changed",
        "lesson_cancelled",
        "lesson_started",
        "lesson_participant_started",
        "lesson_finished",
        "student_arrived_club",
        "student_left_club",
        "participant_added",
        "participant_removed",
        "teacher_replaced",
    ):
        for context in ("student", "guardian", "teacher"):
            rules.append(
                {
                    "event_code": event_code,
                    "recipient_context": context,
                    "offset_minutes": -1,
                    "enabled": True,
                    "requires_confirmation": False,
                    "priority": "high"
                    if event_code in {"lesson_cancelled", "schedule_changed", "teacher_replaced"}
                    else "normal",
                    "quiet_hours_policy": "bypass"
                    if event_code in {"lesson_cancelled", "teacher_replaced"}
                    else "defer",
                    "quiet_start": "22:00",
                    "quiet_end": "08:00",
                    "configuration": {},
                    "updated_at": now,
                }
            )
    bind.execute(
        sa.insert(
            sa.table(
                "notification_global_rules",
                sa.column("event_code", sa.String()),
                sa.column("recipient_context", sa.String()),
                sa.column("offset_minutes", sa.Integer()),
                sa.column("enabled", sa.Boolean()),
                sa.column("requires_confirmation", sa.Boolean()),
                sa.column("priority", sa.String()),
                sa.column("quiet_hours_policy", sa.String()),
                sa.column("quiet_start", sa.String()),
                sa.column("quiet_end", sa.String()),
                sa.column("configuration", sa.JSON()),
                sa.column("updated_at", sa.DateTime(timezone=True)),
            )
        ),
        rules,
    )


def downgrade() -> None:
    raise RuntimeError(
        "Communications migration preserves business history and cannot be downgraded"
    )
