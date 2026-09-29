from __future__ import annotations

import asyncio
from datetime import timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import Person, utcnow
from .learning_models import (
    AdminNotification,
    LessonParticipant,
    NotificationJob,
    PersonMaxIdentity,
)
from .max_api import MaxApiClient, MaxApiError

log = structlog.get_logger()


class LearningNotificationWorker:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        api: MaxApiClient,
        poll_seconds: float = 5.0,
        max_attempts: int = 5,
    ) -> None:
        self.sessions = sessions
        self.api = api
        self.poll_seconds = poll_seconds
        self.max_attempts = max_attempts

    async def run(self) -> None:
        await self.recover_interrupted()
        while True:
            handled = await self.process_one()
            if not handled:
                await asyncio.sleep(self.poll_seconds)

    async def recover_interrupted(self) -> None:
        async with self.sessions() as session:
            await session.execute(
                update(NotificationJob)
                .where(NotificationJob.status == "processing")
                .values(status="retry", scheduled_at=utcnow(), updated_at=utcnow())
            )
            await session.commit()

    async def process_one(self) -> bool:
        now = utcnow()
        async with self.sessions() as session:
            job = await session.scalar(
                select(NotificationJob)
                .where(
                    NotificationJob.status.in_(["pending", "retry"]),
                    NotificationJob.scheduled_at <= now,
                )
                .order_by(NotificationJob.scheduled_at, NotificationJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return False
            person = await session.get(Person, job.recipient_person_id)
            identity = await session.get(PersonMaxIdentity, job.recipient_person_id)
            if (
                person is None
                or person.archived_at is not None
                or not person.active
                or identity is None
                or identity.max_user_id is None
            ):
                job.status = "cancelled"
                job.last_error = "Получатель не авторизован в MAX"
                job.updated_at = utcnow()
                await session.commit()
                return True
            job.status = "processing"
            job.attempts += 1
            job.updated_at = utcnow()
            text = str(job.payload.get("text") or "Уведомление КРиТ")
            if job.payload.get("template") == "teacher_reminder" and job.lesson_id is not None:
                student_names = list(
                    (
                        await session.scalars(
                            select(LessonParticipant.person_name_snapshot)
                            .where(
                                LessonParticipant.lesson_id == job.lesson_id,
                                LessonParticipant.attendance_status != "excused",
                            )
                            .order_by(LessonParticipant.person_name_snapshot)
                        )
                    ).all()
                )
                text += "\n\nУченики:\n" + (
                    "\n".join(student_names) if student_names else "Нет участников"
                )
            await session.commit()
            job_id = job.id
            user_id = identity.max_user_id
        try:
            await self.api.send_text(user_id=user_id, text=text)
        except (MaxApiError, OSError, TimeoutError) as exc:
            async with self.sessions() as session:
                job = await session.get(NotificationJob, job_id)
                if job is None:
                    return True
                transient = not isinstance(exc, MaxApiError) or exc.transient
                if transient and job.attempts < self.max_attempts:
                    job.status = "retry"
                    job.scheduled_at = utcnow() + timedelta(
                        seconds=min(300, 5 * (2 ** (job.attempts - 1)))
                    )
                else:
                    job.status = "failed"
                    session.add(
                        AdminNotification(
                            kind="max_notification_failed",
                            title="Не отправлено уведомление MAX",
                            message=(f"Получатель #{job.recipient_person_id}: {str(exc)[:500]}"),
                            lesson_id=job.lesson_id,
                        )
                    )
                job.last_error = str(exc)[:2000]
                job.updated_at = utcnow()
                await session.commit()
            log.warning("learning_notification_failed", job_id=job_id, error=str(exc))
            return True
        async with self.sessions() as session:
            job = await session.get(NotificationJob, job_id)
            if job is not None:
                job.status = "sent"
                job.sent_at = utcnow()
                job.last_error = None
                job.updated_at = utcnow()
                await session.commit()
        return True
