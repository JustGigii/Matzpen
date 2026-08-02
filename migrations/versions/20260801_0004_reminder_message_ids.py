"""Store Telegram message IDs for reminder-card synchronization.

Revision ID: 20260801_0004
Revises: 20260801_0003
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260801_0004"
down_revision: str | None = "20260801_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reminders") as batch:
        batch.add_column(sa.Column("telegram_message_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reminders") as batch:
        batch.drop_column("telegram_message_id")
