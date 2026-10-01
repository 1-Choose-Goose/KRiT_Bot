from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


class NotificationGlobalRule(Base):
    __tablename__ = "notification_global_rules"
    __table_args__ = (
        UniqueConstraint(
            "event_code", "recipient_context", "offset_minutes", name="uq_global_rule_key"
        ),
        CheckConstraint("recipient_context IN ('*','student','guardian','teacher')"),
        CheckConstraint("priority IN ('low','normal','high')"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    recipient_context: Mapped[str] = mapped_column(String(16), nullable=False, default="*")
    offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    quiet_hours_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="defer")
    quiet_start: Mapped[str | None] = mapped_column(String(5))
    quiet_end: Mapped[str | None] = mapped_column(String(5))
    configuration: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PersonNotificationOverride(Base):
    __tablename__ = "person_notification_overrides"
    __table_args__ = (
        UniqueConstraint(
            "person_id",
            "recipient_context",
            "event_code",
            "offset_minutes",
            name="uq_person_notification_override",
        ),
        CheckConstraint("state IN ('inherit','on','off')"),
        CheckConstraint("recipient_context IN ('student','guardian','teacher')"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_context: Mapped[str] = mapped_column(String(16), nullable=False)
    event_code: Mapped[str] = mapped_column(String(64), nullable=False)
    offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    state: Mapped[str] = mapped_column(String(12), nullable=False, default="inherit")
    configuration: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GuardianNotificationOverride(Base):
    __tablename__ = "guardian_notification_overrides"
    __table_args__ = (
        UniqueConstraint(
            "guardian_person_id",
            "student_person_id",
            "event_code",
            "offset_minutes",
            name="uq_guardian_notification_override",
        ),
        CheckConstraint("state IN ('inherit','on','off')"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guardian_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_code: Mapped[str] = mapped_column(String(64), nullable=False)
    offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    state: Mapped[str] = mapped_column(String(12), nullable=False, default="inherit")
    configuration: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommunicationCampaign(Base):
    __tablename__ = "communication_campaigns"
    __table_args__ = (
        CheckConstraint(
            "campaign_type IN ('manual_message','custom_poll',"
            "'schedule_publication','schedule_change')"
        ),
        CheckConstraint("status IN ('draft','scheduled','sending','completed','partial','failed')"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), index=True
    )
    campaign_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(250), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommunicationThread(Base):
    __tablename__ = "communication_threads"
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_message_preview: Mapped[str | None] = mapped_column(String(240))
    admin_unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    admin_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InteractionRequest(Base):
    __tablename__ = "interaction_requests"
    __table_args__ = (
        CheckConstraint("request_type IN ('yes_no','lesson_confirmation','custom')"),
        CheckConstraint("status IN ('draft','active','answered','expired','cancelled')"),
        Index("ix_interaction_request_expiry", "status", "expires_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_context: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), index=True
    )
    related_lesson_id: Mapped[int | None] = mapped_column(
        ForeignKey("learning_lessons.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("communication_campaigns.id", ondelete="SET NULL"), index=True
    )
    created_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    request_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    expects_reason_from_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL"), index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InteractionRequestLesson(Base):
    __tablename__ = "interaction_request_lessons"
    request_id: Mapped[int] = mapped_column(
        ForeignKey("interaction_requests.id", ondelete="CASCADE"), primary_key=True
    )
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("learning_lessons.id", ondelete="CASCADE"), primary_key=True
    )
    lesson_revision: Mapped[int] = mapped_column(Integer, nullable=False)


class InteractionResponse(Base):
    __tablename__ = "interaction_responses"
    __table_args__ = (
        UniqueConstraint(
            "request_id",
            "respondent_person_id",
            "respondent_context",
            name="uq_interaction_current_response",
        ),
        CheckConstraint("answer IN ('yes','no','partial')"),
        Index("ix_interaction_response_request_person", "request_id", "respondent_person_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("interaction_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    respondent_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    respondent_context: Mapped[str] = mapped_column(String(16), nullable=False)
    answer: Mapped[str] = mapped_column(String(16), nullable=False)
    lesson_answers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InteractionResponseHistory(Base):
    __tablename__ = "interaction_response_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    response_id: Mapped[int] = mapped_column(
        ForeignKey("interaction_responses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_answer: Mapped[str | None] = mapped_column(String(16))
    new_answer: Mapped[str] = mapped_column(String(16), nullable=False)
    old_lesson_answers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    new_lesson_answers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LessonAttendanceIntent(Base):
    __tablename__ = "lesson_attendance_intents"
    __table_args__ = (
        UniqueConstraint(
            "lesson_id", "student_person_id", "lesson_revision", name="uq_lesson_intent_revision"
        ),
        CheckConstraint(
            "status IN ('pending','confirmed','declined','no_response',"
            "'needs_reconfirmation','conflict')"
        ),
        Index("ix_lesson_intent_lesson_student", "lesson_id", "student_person_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("learning_lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    last_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("interaction_requests.id", ondelete="SET NULL")
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SchedulePublication(Base):
    __tablename__ = "schedule_publications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_from: Mapped[date] = mapped_column(Date, nullable=False)
    period_to: Mapped[date] = mapped_column(Date, nullable=False)
    created_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("communication_campaigns.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScheduleRecipientSnapshot(Base):
    __tablename__ = "schedule_recipient_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "recipient_person_id",
            "recipient_context",
            "subject_person_id",
            "lesson_id",
            name="uq_schedule_recipient_lesson",
        ),
        Index("ix_schedule_snapshot_recipient_lesson", "recipient_person_id", "lesson_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedule_publications.id", ondelete="SET NULL"), index=True
    )
    recipient_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_context: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("learning_lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommunicationMessage(Base):
    __tablename__ = "communication_messages"
    __table_args__ = (
        CheckConstraint("direction IN ('inbound','outbound')"),
        CheckConstraint(
            "delivery_status IN ('received','pending','sending','sent','failed','unavailable')"
        ),
        Index("ix_communication_message_person_created", "person_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    max_message_id: Mapped[str | None] = mapped_column(String(180), index=True)
    delivery_status: Mapped[str] = mapped_column(String(16), nullable=False)
    related_lesson_id: Mapped[int | None] = mapped_column(
        ForeignKey("learning_lessons.id", ondelete="SET NULL"), index=True
    )
    interaction_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("interaction_requests.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("communication_campaigns.id", ondelete="SET NULL"), index=True
    )
    outbox_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("learning_notification_jobs.id", ondelete="SET NULL"), unique=True
    )
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MaxRegistrationPending(Base):
    __tablename__ = "max_registration_pending"
    __table_args__ = (Index("ix_max_registration_pending_expiry", "expires_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    max_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    verified_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    required_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
