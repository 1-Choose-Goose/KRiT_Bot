from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
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


class OnlineAuthChallenge(Base):
    __tablename__ = "online_auth_challenges"
    __table_args__ = (
        Index("ix_online_auth_person_created", "person_id", "created_at"),
        Index("ix_online_auth_ip_created", "request_ip_hash", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlineWebSession(Base):
    __tablename__ = "online_web_sessions"
    __table_args__ = (Index("ix_online_web_session_person_active", "person_id", "revoked_at"),)

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlineLessonSession(Base):
    __tablename__ = "online_lesson_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting','active','completed','archived')",
            name="ck_online_lesson_session_status",
        ),
        CheckConstraint(
            "student_drawing_mode IN ('teacher_only','selected','collaboration','locked')",
            name="ck_online_student_drawing_mode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("learning_lessons.id", ondelete="CASCADE"), unique=True, index=True
    )
    room_key: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="waiting", index=True)
    student_drawing_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="teacher_only"
    )
    board_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_page_id: Mapped[str] = mapped_column(String(64), nullable=False, default="main")
    chat_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlineLessonParticipant(Base):
    __tablename__ = "online_lesson_participants"
    __table_args__ = (
        UniqueConstraint("online_session_id", "person_id", name="uq_online_participant"),
        Index("ix_online_participant_connected", "online_session_id", "left_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    online_session_id: Mapped[int] = mapped_column(
        ForeignKey("online_lesson_sessions.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"), index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hand_raised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    subgroup_key: Mapped[str | None] = mapped_column(String(36), index=True)


class OnlineBoardPermission(Base):
    __tablename__ = "online_board_permissions"
    __table_args__ = (
        UniqueConstraint("online_session_id", "person_id", name="uq_online_board_permission"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    online_session_id: Mapped[int] = mapped_column(
        ForeignKey("online_lesson_sessions.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"), index=True)
    can_draw: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    changed_by_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlineBoardOperation(Base):
    __tablename__ = "online_board_operations"
    __table_args__ = (
        UniqueConstraint("online_session_id", "operation_id", name="uq_online_board_operation"),
        UniqueConstraint("online_session_id", "revision", name="uq_online_board_revision"),
        Index("ix_online_board_page_revision", "online_session_id", "page_id", "revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    online_session_id: Mapped[int] = mapped_column(
        ForeignKey("online_lesson_sessions.id", ondelete="CASCADE"), index=True
    )
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    page_id: Mapped[str] = mapped_column(String(64), nullable=False, default="main")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actor_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlineBoardSnapshot(Base):
    __tablename__ = "online_board_snapshots"
    __table_args__ = (
        UniqueConstraint("online_session_id", "revision", name="uq_online_board_snapshot_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    online_session_id: Mapped[int] = mapped_column(
        ForeignKey("online_lesson_sessions.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlineChatMessage(Base):
    __tablename__ = "online_chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    online_session_id: Mapped[int] = mapped_column(
        ForeignKey("online_lesson_sessions.id", ondelete="CASCADE"), index=True
    )
    author_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), index=True
    )
    body: Mapped[str] = mapped_column(String(2000), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlineLessonAsset(Base):
    __tablename__ = "online_lesson_assets"
    __table_args__ = (
        UniqueConstraint("online_session_id", "stored_name", name="uq_online_asset_stored_name"),
        Index("ix_online_asset_room_created", "online_session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    online_session_id: Mapped[int] = mapped_column(
        ForeignKey("online_lesson_sessions.id", ondelete="CASCADE"), index=True
    )
    uploader_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), index=True
    )
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(80), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlineAssignment(Base):
    __tablename__ = "online_assignments"
    __table_args__ = (
        CheckConstraint("kind IN ('lesson','homework')", name="ck_online_assignment_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    online_session_id: Mapped[int] = mapped_column(
        ForeignKey("online_lesson_sessions.id", ondelete="CASCADE"), index=True
    )
    author_person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="lesson")
    title: Mapped[str] = mapped_column(String(250), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlineAssignmentRecipient(Base):
    __tablename__ = "online_assignment_recipients"

    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("online_assignments.id", ondelete="CASCADE"), primary_key=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="assigned", index=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OnlineAssignmentSubmission(Base):
    __tablename__ = "online_assignment_submissions"
    __table_args__ = (
        UniqueConstraint("assignment_id", "person_id", name="uq_online_assignment_submission"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("online_assignments.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"), index=True)
    answer_text: Mapped[str | None] = mapped_column(Text)
    board_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    teacher_comment: Mapped[str | None] = mapped_column(Text)
    grade: Mapped[str | None] = mapped_column(String(32))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlinePoll(Base):
    __tablename__ = "online_polls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    online_session_id: Mapped[int] = mapped_column(
        ForeignKey("online_lesson_sessions.id", ondelete="CASCADE"), index=True
    )
    author_person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="RESTRICT"))
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    options: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    show_results: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlinePollResponse(Base):
    __tablename__ = "online_poll_responses"

    poll_id: Mapped[int] = mapped_column(
        ForeignKey("online_polls.id", ondelete="CASCADE"), primary_key=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True
    )
    option_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OnlineAuditEvent(Base):
    __tablename__ = "online_audit_events"
    __table_args__ = (Index("ix_online_audit_room_created", "online_session_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    online_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("online_lesson_sessions.id", ondelete="SET NULL"), index=True
    )
    actor_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
