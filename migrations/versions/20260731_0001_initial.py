"""Create first-milestone domain tables.

Revision ID: 20260731_0001
Revises: None
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from personal_agent.domain.database import JSONData

revision: str = "20260731_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "whatsapp",
                "telegram",
                "calendar",
                "shortcut",
                "gmail",
                "desktop",
                name="event_source",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("source_account", sa.String(length=255), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("inbound", "outbound", "internal", name="event_direction", native_enum=False),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_external_id", sa.String(length=255), nullable=True),
        sa.Column("actor_display_name", sa.String(length=255), nullable=True),
        sa.Column("conversation_external_id", sa.String(length=255), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("payload_json", JSONData(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column(
            "sensitivity",
            sa.Enum("normal", "personal", "sensitive", name="sensitivity", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "processing_status",
            sa.Enum("pending", "processed", "failed", name="processing_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_events_source_account_dedupe",
        "events",
        ["source", "source_account", "dedupe_key"],
        unique=True,
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "done", "cancelled", name="task_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_event_id", sa.Uuid(), nullable=True),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "commitments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "direction",
            sa.Enum(
                "user_promised",
                "other_promised",
                name="commitment_direction",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "action_type",
            sa.Enum(
                "call",
                "message",
                "send",
                "meet",
                "pay",
                "other",
                name="action_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "detected",
                "scheduled",
                "done",
                "cancelled",
                "overdue",
                name="commitment_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reminder_lead_minutes", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "memory_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("predicate", sa.String(length=500), nullable=False),
        sa.Column("value_json", JSONData(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "proposed",
                "confirmed",
                "rejected",
                "expired",
                name="memory_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "sensitivity",
            sa.Enum(
                "normal",
                "personal",
                "sensitive",
                name="memory_sensitivity",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("source_event_ids", JSONData(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(length=100), nullable=False),
        sa.Column("action_payload", JSONData(), nullable=False),
        sa.Column("risk_class", sa.String(length=50), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                "expired",
                "executed",
                "failed",
                name="approval_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("execute_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_message_id", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=255), nullable=False),
        sa.Column("target", sa.String(length=500), nullable=True),
        sa.Column("policy_decision", sa.String(length=100), nullable=True),
        sa.Column("source_event_id", sa.Uuid(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("result", sa.String(length=100), nullable=False),
        sa.Column("redacted_metadata", JSONData(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["approval_id"], ["approval_requests.id"]),
        sa.ForeignKeyConstraint(["source_event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("approval_requests")
    op.drop_table("memory_facts")
    op.drop_table("commitments")
    op.drop_table("tasks")
    op.drop_index("uq_events_source_account_dedupe", table_name="events")
    op.drop_table("events")
