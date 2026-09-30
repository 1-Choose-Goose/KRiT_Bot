"""Assign qualified teachers to learning subjects."""

import sqlalchemy as sa

from alembic import op

revision = "20260929_subject_teachers_v3"
down_revision = "20260929_learning_process_v2"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "learning_subject_teachers" not in _tables():
        op.create_table(
            "learning_subject_teachers",
            sa.Column(
                "subject_id",
                sa.Integer(),
                sa.ForeignKey("learning_subjects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "teacher_id",
                sa.Integer(),
                sa.ForeignKey("persons.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
        )
        op.create_index(
            "ix_subject_teacher_teacher_id",
            "learning_subject_teachers",
            ["teacher_id"],
        )

    # Preserve known qualifications from existing schedules and group defaults.
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT DISTINCT subject_id, teacher_id
            FROM learning_lessons
            WHERE subject_id IS NOT NULL AND teacher_id IS NOT NULL
            UNION
            SELECT DISTINCT subject_id, teacher_id
            FROM learning_lesson_series
            WHERE subject_id IS NOT NULL AND teacher_id IS NOT NULL
            UNION
            SELECT DISTINCT subject_id, default_teacher_id AS teacher_id
            FROM learning_groups
            WHERE subject_id IS NOT NULL AND default_teacher_id IS NOT NULL
            """
            )
        )
        .fetchall()
    )
    existing = set(
        op.get_bind()
        .execute(sa.text("SELECT subject_id, teacher_id FROM learning_subject_teachers"))
        .fetchall()
    )
    association = sa.table(
        "learning_subject_teachers",
        sa.column("subject_id", sa.Integer()),
        sa.column("teacher_id", sa.Integer()),
    )
    additions = [
        {"subject_id": int(subject_id), "teacher_id": int(teacher_id)}
        for subject_id, teacher_id in rows
        if (subject_id, teacher_id) not in existing
    ]
    if additions:
        op.bulk_insert(association, additions)


def downgrade() -> None:
    raise RuntimeError("Downgrade is disabled to protect production learning history")
