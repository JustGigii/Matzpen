"""Bring early milestone-2 databases to the final lifecycle schema.

Revision ID: 20260801_0003
Revises: 20260801_0002
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260801_0003"
down_revision: str | None = "20260801_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    reminder_columns = {column["name"]: column for column in inspector.get_columns("reminders")}
    reminder_foreign_keys = {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys("reminders")
    }
    with op.batch_alter_table("reminders") as batch:
        if not reminder_columns["commitment_id"]["nullable"]:
            batch.alter_column(
                "commitment_id",
                existing_type=sa.Uuid(),
                nullable=True,
            )
        if "task_id" not in reminder_columns:
            batch.add_column(sa.Column("task_id", sa.Uuid(), nullable=True))
        if ("task_id",) not in reminder_foreign_keys:
            batch.create_foreign_key(
                "fk_reminders_task_id_tasks",
                "tasks",
                ["task_id"],
                ["id"],
            )

    inspector = sa.inspect(bind)
    calendar_columns = {column["name"] for column in inspector.get_columns("calendar_actions")}
    calendar_foreign_keys = {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys("calendar_actions")
    }
    with op.batch_alter_table("calendar_actions") as batch:
        if "approval_id" not in calendar_columns:
            batch.add_column(sa.Column("approval_id", sa.Uuid(), nullable=True))
        if ("approval_id",) not in calendar_foreign_keys:
            batch.create_foreign_key(
                "fk_calendar_actions_approval_id_approval_requests",
                "approval_requests",
                ["approval_id"],
                ["id"],
            )


def downgrade() -> None:
    # Revision 0003 is a compatibility repair for databases that may already have these
    # columns from a later copy of revision 0002. Keeping them makes downgrade to 0002 safe
    # for both histories and matches the current ORM contract.
    pass
