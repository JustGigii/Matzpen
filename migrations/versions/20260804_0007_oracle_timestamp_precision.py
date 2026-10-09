"""Preserve fractional-second precision for UTC timestamps on Oracle.

Revision ID: 20260804_0007
Revises: 20260802_0006
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import oracle

revision: str = "20260804_0007"
down_revision: str | None = "20260802_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UTC_COLUMNS: dict[str, tuple[str, ...]] = {
    "events": ("occurred_at", "received_at", "redacted_at", "revoked_at", "created_at"),
    "memory_facts": ("valid_from", "expires_at", "last_verified_at", "created_at"),
    "morning_briefs": ("triggered_at", "generated_at", "sent_at", "created_at"),
    "persons": ("last_relevant_interaction_at", "updated_at", "created_at"),
    "whatsapp_conversations": (
        "tracking_prompted_at",
        "last_message_at",
        "processing_watermark",
        "updated_at",
        "created_at",
    ),
    "whatsapp_session_states": (
        "connected_at",
        "disconnected_at",
        "last_webhook_at",
        "last_processed_event_at",
        "history_watermark",
        "disconnect_warning_sent_at",
        "archive_refreshed_at",
        "updated_at",
        "created_at",
    ),
    "approval_requests": ("execute_after", "expires_at", "resolved_at", "created_at"),
    "tasks": ("due_at", "updated_at", "created_at"),
    "whatsapp_conversation_buffers": (
        "first_message_at",
        "last_message_at",
        "flush_at",
        "processed_at",
        "updated_at",
        "created_at",
    ),
    "whatsapp_historical_findings": ("resolved_at", "created_at"),
    "audit_logs": ("timestamp",),
    "commitments": ("due_at", "overdue_at", "last_daily_nag_at", "updated_at", "created_at"),
    "calendar_actions": ("executed_at", "created_at"),
    "reminders": ("scheduled_for", "sent_at", "created_at"),
}


def _change_type(target_type: object, existing_type: object) -> None:
    for table_name, columns in UTC_COLUMNS.items():
        for column_name in columns:
            op.alter_column(
                table_name,
                column_name,
                type_=target_type,
                existing_type=existing_type,
            )


def upgrade() -> None:
    if op.get_bind().dialect.name == "oracle":
        _change_type(oracle.TIMESTAMP(), oracle.DATE())


def downgrade() -> None:
    if op.get_bind().dialect.name == "oracle":
        _change_type(oracle.DATE(), oracle.TIMESTAMP())
