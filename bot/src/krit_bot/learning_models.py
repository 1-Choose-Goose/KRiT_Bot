from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


class PersonMaxIdentity(Base):
    """MAX identity separated from the mutable contact phone."""

    __tablename__ = "person_max_identities"
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True
    )
    verified_phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    max_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Subject(Base):
    __tablename__ = "learning_subjects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="#2563eb")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SubjectTeacher(Base):
    __tablename__ = "learning_subject_teachers"
    __table_args__ = (Index("ix_subject_teacher_teacher_id", "teacher_id"),)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("learning_subjects.id", ondelete="CASCADE"), primary_key=True
    )
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Room(Base):
    __tablename__ = "learning_rooms"
    __table_args__ = (CheckConstraint("capacity > 0", name="ck_room_capacity_positive"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StudyGroup(Base):
    __tablename__ = "learning_groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    subject_id: Mapped[int | None] = mapped_column(
        ForeignKey("learning_subjects.id", ondelete="SET NULL"), index=True
    )
    default_teacher_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL"), index=True
    )
    default_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GroupMembership(Base):
    __tablename__ = "learning_group_memberships"
    __table_args__ = (
        CheckConstraint("end_at IS NULL OR end_at > start_at", name="ck_group_membership_period"),
        Index("ix_group_membership_active", "group_id", "person_id", "end_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("learning_groups.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), index=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LessonSeries(Base):
    __tablename__ = "learning_lesson_series"
    __table_args__ = (CheckConstraint("interval_weeks > 0", name="ck_series_interval_positive"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("learning_subjects.id", ondelete="RESTRICT"), index=True
    )
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), index=True
    )
    room_id: Mapped[int] = mapped_column(
        ForeignKey("learning_rooms.id", ondelete="RESTRICT"), index=True
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("learning_groups.id", ondelete="SET NULL"), index=True
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    interval_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Lesson(Base):
    __tablename__ = "learning_lessons"
    __table_args__ = (
        UniqueConstraint(
            "series_id",
            "series_occurrence_index",
            name="uq_lesson_series_occurrence",
        ),
        CheckConstraint("end_at > start_at", name="ck_lesson_period"),
        CheckConstraint(
            "status IN ('planned','scheduled','in_progress','completed','cancelled')",
            name="ck_lesson_status",
        ),
        Index("ix_lesson_room_period", "room_id", "start_at", "end_at"),
        Index("ix_lesson_teacher_period", "teacher_id", "start_at", "end_at"),
        Index("ix_lesson_status_start", "status", "start_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("learning_lesson_series.id", ondelete="SET NULL"), index=True
    )
    series_occurrence_index: Mapped[int | None] = mapped_column(Integer)
    series_exception: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("learning_subjects.id", ondelete="RESTRICT"), index=True
    )
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), index=True
    )
    room_id: Mapped[int] = mapped_column(
        ForeignKey("learning_rooms.id", ondelete="RESTRICT"), index=True
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("learning_groups.id", ondelete="SET NULL"), index=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")
    cancelled_reason: Mapped[str | None] = mapped_column(String(500))
    completion_type: Mapped[str | None] = mapped_column(String(16))
    completion_reason: Mapped[str | None] = mapped_column(String(500))
    completion_public_comment: Mapped[str | None] = mapped_column(String(500))
    teacher_name_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)
    room_name_snapshot: Mapped[str] = mapped_column(String(120), nullable=False)
    subject_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LessonParticipant(Base):
    __tablename__ = "learning_lesson_participants"
    __table_args__ = (
        UniqueConstraint("lesson_id", "person_id", name="uq_lesson_participant"),
        CheckConstraint(
            "attendance_status IN ('expected','present','late','absent','left_early','excused')",
            name="ck_lesson_attendance_status",
        ),
        CheckConstraint(
            "cancelled_by IS NULL OR cancelled_by IN ('student','guardian','administrator')",
            name="ck_lesson_participant_cancelled_by",
        ),
        Index("ix_participant_person_lesson", "person_id", "lesson_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("learning_lessons.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), index=True
    )
    person_name_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)
    attendance_status: Mapped[str] = mapped_column(String(20), nullable=False, default="expected")
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    late_minutes: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(500))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(20))
    cancelled_by_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL"), index=True
    )
    cancelled_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), index=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))
    early_leave_reason: Mapped[str | None] = mapped_column(String(500))


class LessonTeacherSegment(Base):
    __tablename__ = "learning_lesson_teacher_segments"
    __table_args__ = (
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at", name="ck_teacher_segment_period"
        ),
        CheckConstraint(
            "segment_type IN ('primary','substitute')",
            name="ck_teacher_segment_type",
        ),
        Index("ix_teacher_segment_lesson_period", "lesson_id", "started_at"),
        Index("ix_teacher_segment_teacher_period", "teacher_person_id", "started_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("learning_lessons.id", ondelete="CASCADE"), index=True
    )
    teacher_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), index=True
    )
    teacher_name_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    segment_type: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    created_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ClubPresenceSession(Base):
    __tablename__ = "learning_club_presence_sessions"
    __table_args__ = (
        CheckConstraint("left_at IS NULL OR left_at >= arrived_at", name="ck_presence_period"),
        Index("ix_presence_person_open", "person_id", "left_at"),
        Index(
            "uq_presence_one_open_per_person",
            "person_id",
            unique=True,
            sqlite_where=text("left_at IS NULL"),
            postgresql_where=text("left_at IS NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), index=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="management")
    arrived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    arrived_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    left_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )


class NotificationJob(Base):
    __tablename__ = "learning_notification_jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_learning_notification_dedupe"),
        CheckConstraint(
            "status IN ('pending','processing','sent','retry','failed','cancelled')",
            name="ck_learning_notification_status",
        ),
        Index("ix_learning_notification_due", "status", "scheduled_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(180), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    lesson_id: Mapped[int | None] = mapped_column(
        ForeignKey("learning_lessons.id", ondelete="CASCADE"), index=True
    )
    recipient_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), index=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_message_id: Mapped[str | None] = mapped_column(String(180))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminNotification(Base):
    __tablename__ = "learning_admin_notifications"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_admin_notification_dedupe"),
        Index(
            "uq_admin_notification_open_condition",
            "condition_key",
            unique=True,
            sqlite_where=text("condition_key IS NOT NULL AND resolved_at IS NULL"),
            postgresql_where=text("condition_key IS NOT NULL AND resolved_at IS NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(180), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    lesson_id: Mapped[int | None] = mapped_column(
        ForeignKey("learning_lessons.id", ondelete="CASCADE"), index=True
    )
    condition_key: Mapped[str | None] = mapped_column(String(180), index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "learning_audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
