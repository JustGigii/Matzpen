"""Add durable opt-in state for WhatsApp group tracking.

Revision ID: 20260802_0006
Revises: 20260801_0005
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0006"
down_revision: str | None = "20260801_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("whatsapp_conversations") as batch:
        batch.add_column(
            sa.Column(
                "tracking_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("tracking_prompted_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("whatsapp_conversations") as batch:
        batch.drop_column("tracking_prompted_at")
        batch.drop_column("tracking_enabled")
