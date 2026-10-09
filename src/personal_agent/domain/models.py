import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Enum as SqlEnum
from sqlalchemy import Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent.core.time import utc_now
from personal_agent.domain.database import Base, JSONData, UTCDateTime
from personal_agent.domain.enums import (
    ActionType,
    ApprovalStatus,
    CalendarActionStatus,
    CommitmentDirection,
    CommitmentStatus,
    EventDirection,
    EventSource,
    HistoricalFindingStatus,
    MemoryStatus,
    ProcessingStatus,
    ReminderKind,
    ReminderStatus,
    Sensitivity,
    TaskStatus,
    WhatsAppBufferStatus,
    WhatsAppConversationType,
    WhatsAppInitialReviewStatus,
    WhatsAppSessionStatus,
)


def enum_type(enum_class: type[Any], name: str) -> SqlEnum:
    return SqlEnum(
        enum_class,
        name=name,
        native_enum=False,
        values_callable=lambda members: [member.value for member in members],
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class Event(TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        Index(
            "uq_events_source_account_dedupe",
            "source",
            "source_account",
            "dedupe_key",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source: Mapped[EventSource] = mapped_column(enum_type(EventSource, "event_source"))
    source_account: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(100))
    direction: Mapped[EventDirection] = mapped_column(enum_type(EventDirection, "event_direction"))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime())
    received_at: Mapped[datetime] = mapped_column(UTCDateTime())
    actor_external_id: Mapped[str | None] = mapped_column(String(255))
    actor_display_name: Mapped[str | None] = mapped_column(String(255))
    conversation_external_id: Mapped[str | None] = mapped_column(String(255))
    content_text: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONData(), default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(255))
    sensitivity: Mapped[Sensitivity] = mapped_column(enum_type(Sensitivity, "sensitivity"))
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        enum_type(ProcessingStatus, "processing_status"), default=ProcessingStatus.PENDING
    )
    redacted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    supersedes_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id"))


class Person(TimestampMixin, Base):
    __tablename__ = "persons"
    __table_args__ = (
        Index("uq_persons_channel_external_id", "channel", "external_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    channel: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    aliases: Mapped[list[str]] = mapped_column(JSONData(), default=list)
    conversation_ids: Mapped[list[str]] = mapped_column(JSONData(), default=list)
    relationship: Mapped[str | None] = mapped_column(String(100))
    operational_facts: Mapped[dict[str, Any]] = mapped_column(JSONData(), default=dict)
    source_event_ids: Mapped[list[str]] = mapped_column(JSONData(), default=list)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    last_relevant_interaction_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("uq_tasks_dedupe_key", "dedupe_key", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        enum_type(TaskStatus, "task_status"), default=TaskStatus.PENDING
    )
    priority: Mapped[int] = mapped_column(default=0)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id"))
    person_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class Commitment(TimestampMixin, Base):
    __tablename__ = "commitments"
    __table_args__ = (Index("uq_commitments_dedupe_key", "dedupe_key", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    direction: Mapped[CommitmentDirection] = mapped_column(
        enum_type(CommitmentDirection, "commitment_direction")
    )
    action_type: Mapped[ActionType] = mapped_column(enum_type(ActionType, "action_type"))
    summary: Mapped[str] = mapped_column(String(1000))
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    person_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    source_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    status: Mapped[CommitmentStatus] = mapped_column(
        enum_type(CommitmentStatus, "commitment_status"), default=CommitmentStatus.DETECTED
    )
    confidence: Mapped[float] = mapped_column(Float)
    reminder_lead_minutes: Mapped[int] = mapped_column(default=5)
    resolution_source: Mapped[str | None] = mapped_column(String(50))
    overdue_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_daily_nag_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source_approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approval_requests.id"))
    calendar_action_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class MemoryFact(TimestampMixin, Base):
    __tablename__ = "memory_facts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    category: Mapped[str] = mapped_column(String(100))
    subject: Mapped[str] = mapped_column(String(500))
    predicate: Mapped[str] = mapped_column(String(500))
    value_json: Mapped[dict[str, Any]] = mapped_column(JSONData())
    status: Mapped[MemoryStatus] = mapped_column(enum_type(MemoryStatus, "memory_status"))
    confidence: Mapped[float] = mapped_column(Float)
    sensitivity: Mapped[Sensitivity] = mapped_column(enum_type(Sensitivity, "memory_sensitivity"))
    source_event_ids: Mapped[list[str]] = mapped_column(JSONData())
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_verified_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class ApprovalRequest(TimestampMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (Index("uq_approval_requests_dedupe_key", "dedupe_key", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    action_type: Mapped[str] = mapped_column(String(100))
    action_payload: Mapped[dict[str, Any]] = mapped_column(JSONData())
    risk_class: Mapped[str] = mapped_column(String(50))
    status: Mapped[ApprovalStatus] = mapped_column(
        enum_type(ApprovalStatus, "approval_status"), default=ApprovalStatus.PENDING
    )
    execute_after: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    telegram_message_id: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id"))
    dedupe_key: Mapped[str | None] = mapped_column(String(255))


class Reminder(TimestampMixin, Base):
    __tablename__ = "reminders"
    __table_args__ = (Index("uq_reminders_dedupe_key", "dedupe_key", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    commitment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("commitments.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    kind: Mapped[ReminderKind] = mapped_column(enum_type(ReminderKind, "reminder_kind"))
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[ReminderStatus] = mapped_column(
        enum_type(ReminderStatus, "reminder_status"), default=ReminderStatus.PENDING
    )
    dedupe_key: Mapped[str] = mapped_column(String(255))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    telegram_message_id: Mapped[str | None] = mapped_column(String(255))


class CalendarAction(TimestampMixin, Base):
    __tablename__ = "calendar_actions"
    __table_args__ = (Index("uq_calendar_actions_dedupe_key", "dedupe_key", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    commitment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("commitments.id"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approval_requests.id"))
    operation: Mapped[str] = mapped_column(String(50))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONData())
    status: Mapped[CalendarActionStatus] = mapped_column(
        enum_type(CalendarActionStatus, "calendar_action_status")
    )
    google_event_id: Mapped[str | None] = mapped_column(String(255))
    google_recurrence_id: Mapped[str | None] = mapped_column(String(255))
    dedupe_key: Mapped[str] = mapped_column(String(255))
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MorningBrief(TimestampMixin, Base):
    __tablename__ = "morning_briefs"
    __table_args__ = (Index("uq_morning_briefs_local_date", "local_date", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    local_date: Mapped[date] = mapped_column()
    trigger_source: Mapped[str] = mapped_column(String(100))
    triggered_at: Mapped[datetime] = mapped_column(UTCDateTime())
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    force_requested: Mapped[bool] = mapped_column(default=False)


class WhatsAppSessionState(TimestampMixin, Base):
    __tablename__ = "whatsapp_session_states"

    session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    api_reachable: Mapped[bool] = mapped_column(default=False)
    status: Mapped[WhatsAppSessionStatus] = mapped_column(
        enum_type(WhatsAppSessionStatus, "whatsapp_session_status"),
        default=WhatsAppSessionStatus.UNKNOWN,
    )
    connected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    disconnected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_webhook_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_processed_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    history_watermark: Mapped[datetime | None] = mapped_column(UTCDateTime())
    initial_review_status: Mapped[WhatsAppInitialReviewStatus] = mapped_column(
        enum_type(WhatsAppInitialReviewStatus, "whatsapp_initial_review_status"),
        default=WhatsAppInitialReviewStatus.PENDING,
    )
    incident_id: Mapped[str | None] = mapped_column(String(255))
    disconnect_warning_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    archive_state_reliable: Mapped[bool] = mapped_column(default=False)
    archive_refreshed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    relink_required: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class WhatsAppConversation(TimestampMixin, Base):
    __tablename__ = "whatsapp_conversations"
    __table_args__ = (
        Index(
            "uq_whatsapp_conversations_session_chat",
            "session_id",
            "external_chat_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(String(255))
    external_chat_id: Mapped[str] = mapped_column(String(255))
    chat_type: Mapped[WhatsAppConversationType] = mapped_column(
        enum_type(WhatsAppConversationType, "whatsapp_conversation_type")
    )
    display_name: Mapped[str | None] = mapped_column(String(255))
    archived: Mapped[bool] = mapped_column(default=False)
    ignored: Mapped[bool] = mapped_column(default=False)
    tracking_enabled: Mapped[bool] = mapped_column(default=False)
    tracking_prompted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    processing_watermark: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class WhatsAppConversationBuffer(TimestampMixin, Base):
    __tablename__ = "whatsapp_conversation_buffers"
    __table_args__ = (Index("uq_whatsapp_conversation_buffers_dedupe", "dedupe_key", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("whatsapp_conversations.id"))
    first_message_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_message_at: Mapped[datetime] = mapped_column(UTCDateTime())
    flush_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[WhatsAppBufferStatus] = mapped_column(
        enum_type(WhatsAppBufferStatus, "whatsapp_buffer_status"),
        default=WhatsAppBufferStatus.PENDING,
    )
    urgent: Mapped[bool] = mapped_column(default=False)
    event_ids: Mapped[list[str]] = mapped_column(JSONData(), default=list)
    dedupe_key: Mapped[str] = mapped_column(String(255))
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class WhatsAppHistoricalFinding(TimestampMixin, Base):
    __tablename__ = "whatsapp_historical_findings"
    __table_args__ = (Index("uq_whatsapp_historical_findings_dedupe", "dedupe_key", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(String(255))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("whatsapp_conversations.id")
    )
    source_event_ids: Mapped[list[str]] = mapped_column(JSONData())
    interpretation_payload: Mapped[dict[str, Any]] = mapped_column(JSONData())
    status: Mapped[HistoricalFindingStatus] = mapped_column(
        enum_type(HistoricalFindingStatus, "whatsapp_historical_finding_status"),
        default=HistoricalFindingStatus.PENDING,
    )
    confidence: Mapped[float] = mapped_column(Float)
    dedupe_key: Mapped[str] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(255))
    target: Mapped[str | None] = mapped_column(String(500))
    policy_decision: Mapped[str | None] = mapped_column(String(100))
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approval_requests.id"))
    result: Mapped[str] = mapped_column(String(100))
    redacted_metadata: Mapped[dict[str, Any]] = mapped_column(JSONData(), default=dict)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
