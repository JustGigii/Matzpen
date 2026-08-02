"""Add durable read-only WhatsApp state, batching, history, and people context.

Revision ID: 20260801_0005
Revises: 20260801_0004
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260801_0005"
down_revision: str | None = "20260801_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("supersedes_event_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_events_supersedes_event_id_events",
            "events",
            ["supersedes_event_id"],
            ["id"],
        )

    op.create_table(
        "persons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("conversation_ids", sa.JSON(), nullable=False),
        sa.Column("relationship", sa.String(length=100), nullable=True),
        sa.Column("operational_facts", sa.JSON(), nullable=False),
        sa.Column("source_event_ids", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("last_relevant_interaction_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_persons_channel_external_id",
        "persons",
        ["channel", "external_id"],
        unique=True,
    )

    op.create_table(
        "whatsapp_session_states",
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("api_reachable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "status",
            sa.Enum(
                "unknown",
                "authenticating",
                "qr_required",
                "connected",
                "disconnected",
                "reconnecting",
                "failed",
                name="whatsapp_session_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_webhook_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_processed_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("history_watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "initial_review_status",
            sa.Enum(
                "pending",
                "running",
                "completed",
                "failed",
                name="whatsapp_initial_review_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("incident_id", sa.String(length=255), nullable=True),
        sa.Column("disconnect_warning_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_state_reliable", sa.Boolean(), nullable=False),
        sa.Column("archive_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("relink_required", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
    )

    op.create_table(
        "whatsapp_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("external_chat_id", sa.String(length=255), nullable=False),
        sa.Column(
            "chat_type",
            sa.Enum(
                "private",
                "group",
                "unknown",
                name="whatsapp_conversation_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("ignored", sa.Boolean(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_whatsapp_conversations_session_chat",
        "whatsapp_conversations",
        ["session_id", "external_chat_id"],
        unique=True,
    )

    op.create_table(
        "whatsapp_conversation_buffers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("first_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("flush_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "processed",
                "cancelled",
                "failed",
                name="whatsapp_buffer_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("urgent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("event_ids", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["whatsapp_conversations.id"],
            name="fk_whatsapp_buffers_conversation_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_whatsapp_conversation_buffers_dedupe",
        "whatsapp_conversation_buffers",
        ["dedupe_key"],
        unique=True,
    )

    op.create_table(
        "whatsapp_historical_findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("source_event_ids", sa.JSON(), nullable=False),
        sa.Column("interpretation_payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "ignored",
                "superseded",
                name="whatsapp_historical_finding_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["whatsapp_conversations.id"],
            name="fk_whatsapp_historical_findings_conversation_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_whatsapp_historical_findings_dedupe",
        "whatsapp_historical_findings",
        ["dedupe_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_whatsapp_historical_findings_dedupe",
        table_name="whatsapp_historical_findings",
    )
    op.drop_table("whatsapp_historical_findings")
    op.drop_index(
        "uq_whatsapp_conversation_buffers_dedupe",
        table_name="whatsapp_conversation_buffers",
    )
    op.drop_table("whatsapp_conversation_buffers")
    op.drop_index(
        "uq_whatsapp_conversations_session_chat",
        table_name="whatsapp_conversations",
    )
    op.drop_table("whatsapp_conversations")
    op.drop_table("whatsapp_session_states")
    op.drop_index("uq_persons_channel_external_id", table_name="persons")
    op.drop_table("persons")
    with op.batch_alter_table("events") as batch:
        batch.drop_constraint("fk_events_supersedes_event_id_events", type_="foreignkey")
        batch.drop_column("supersedes_event_id")
        batch.drop_column("revoked_at")
        batch.drop_column("redacted_at")
