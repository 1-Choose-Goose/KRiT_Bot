"""repair attendance left expected after completed lessons

Revision ID: 20261025_attendance_v14
Revises: 20261021_online_lessons_v13
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261025_attendance_v14"
down_revision: str | None = "20261021_online_lessons_v13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE learning_lesson_participants
        SET attendance_status = 'absent',
            arrived_at = NULL,
            left_at = NULL,
            late_minutes = 0
        WHERE attendance_status = 'expected'
          AND lesson_id IN (
              SELECT id
              FROM learning_lessons
              WHERE status = 'completed'
          )
        """
    )


def downgrade() -> None:
    raise RuntimeError("Downgrade is disabled to protect production learning history")
