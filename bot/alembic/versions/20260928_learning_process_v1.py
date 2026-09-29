"""Adopt the existing KRiT schema as the Alembic baseline."""

from alembic import op
from krit_bot import learning_models as _learning_models  # noqa: F401
from krit_bot.db import Base

revision = "20260928_learning_process_v1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # On an existing production database this is a no-op. On a fresh database it
    # creates the current baseline without a separate legacy bootstrap path.
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    raise RuntimeError("The production baseline cannot be downgraded destructively")
