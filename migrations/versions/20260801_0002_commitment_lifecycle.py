"""Add durable commitment lifecycle, calendar actions, and morning briefs.

Revision ID: 20260801_0002
Revises: 20260731_0001
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260801_0002"
down_revision: str | None = "20260731_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("dedupe_key", sa.String(length=255), nullable=True))
        batch.create_index("uq_tasks_dedupe_key", ["dedupe_key"], unique=True)

    with op.batch_alter_table("approval_requests") as batch:
        batch.add_column(sa.Column("source_event_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("dedupe_key", sa.String(length=255), nullable=True))
        batch.create_foreign_key(
            "fk_approval_requests_source_event_id_events", "events", ["source_event_id"], ["id"]
        )
        batch.create_index("uq_approval_requests_dedupe_key", ["dedupe_key"], unique=True)

    with op.batch_alter_table("commitments") as batch:
        batch.add_column(sa.Column("resolution_source", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("overdue_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_daily_nag_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("source_approval_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("calendar_action_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("dedupe_key", sa.String(length=255), nullable=True))
        batch.create_foreign_key(
            "fk_commitments_source_approval_id_approval_requests",
            "approval_requests",
            ["source_approval_id"],
            ["id"],
        )

    op.execute("UPDATE commitments SET dedupe_key = 'legacy:' || id WHERE dedupe_key IS NULL")
    with op.batch_alter_table("commitments") as batch:
        batch.alter_column("dedupe_key", existing_type=sa.String(length=255), nullable=False)
        batch.create_index("uq_commitments_dedupe_key", ["dedupe_key"], unique=True)

    op.create_table(
        "reminders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("commitment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("before_due", "due", "snooze", name="reminder_kind", native_enum=False),
            nullable=False,
        ),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "sent", "handled", "cancelled", name="reminder_status", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["commitment_id"], ["commitments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_reminders_dedupe_key", "reminders", ["dedupe_key"], unique=True)

    op.create_table(
        "calendar_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("commitment_id", sa.Uuid(), nullable=True),
        sa.Column("operation", sa.String(length=50), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "pending_configuration",
                "executed",
                "failed",
                "cancelled",
                name="calendar_action_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("google_event_id", sa.String(length=255), nullable=True),
        sa.Column("google_recurrence_id", sa.String(length=255), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_event_id"], ["events.id"]),
        sa.ForeignKeyConstraint(["commitment_id"], ["commitments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_calendar_actions_dedupe_key", "calendar_actions", ["dedupe_key"], unique=True
    )

    op.create_table(
        "morning_briefs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("trigger_source", sa.String(length=100), nullable=False),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("force_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_morning_briefs_local_date", "morning_briefs", ["local_date"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_morning_briefs_local_date", table_name="morning_briefs")
    op.drop_table("morning_briefs")
    op.drop_index("uq_calendar_actions_dedupe_key", table_name="calendar_actions")
    op.drop_table("calendar_actions")
    op.drop_index("uq_reminders_dedupe_key", table_name="reminders")
    op.drop_table("reminders")

    with op.batch_alter_table("commitments") as batch:
        batch.drop_index("uq_commitments_dedupe_key")
        batch.drop_constraint(
            "fk_commitments_source_approval_id_approval_requests", type_="foreignkey"
        )
        batch.drop_column("dedupe_key")
        batch.drop_column("calendar_action_id")
        batch.drop_column("source_approval_id")
        batch.drop_column("last_daily_nag_at")
        batch.drop_column("overdue_at")
        batch.drop_column("resolution_source")

    with op.batch_alter_table("approval_requests") as batch:
        batch.drop_index("uq_approval_requests_dedupe_key")
        batch.drop_constraint("fk_approval_requests_source_event_id_events", type_="foreignkey")
        batch.drop_column("dedupe_key")
        batch.drop_column("source_event_id")

    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("uq_tasks_dedupe_key")
        batch.drop_column("dedupe_key")
