import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from personal_agent.domain.database import Base, create_engine, create_session_factory
from personal_agent.domain.enums import (
    ActionType,
    CommitmentDirection,
    CommitmentStatus,
    EventDirection,
    EventSource,
    ProcessingStatus,
    ReminderKind,
    ReminderStatus,
    Sensitivity,
)
from personal_agent.domain.models import Commitment, Event, Reminder
from personal_agent.services.reminders import ReminderService


async def test_failed_telegram_delivery_returns_reminder_to_retry_queue(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'retry.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    now = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    reminder_id = uuid.uuid4()
    notifier = SimpleNamespace(
        reminder=AsyncMock(side_effect=[RuntimeError("telegram unavailable"), "message-1"])
    )
    service = ReminderService(
        factory,
        notifier,
        SimpleNamespace(),
        SimpleNamespace(),
        lambda: now,
    )
    async with factory() as session:
        event = Event(
            source=EventSource.TELEGRAM,
            source_account="bot",
            external_id="retry-source",
            event_type="message.received",
            direction=EventDirection.INBOUND,
            occurred_at=now,
            received_at=now,
            actor_external_id="123",
            actor_display_name="User",
            conversation_external_id="123",
            content_text="תזכיר לי להתקשר",
            payload_json={},
            dedupe_key="retry-source",
            sensitivity=Sensitivity.PERSONAL,
            processing_status=ProcessingStatus.PROCESSED,
        )
        session.add(event)
        await session.flush()
        commitment = Commitment(
            direction=CommitmentDirection.USER_PROMISED,
            action_type=ActionType.CALL,
            summary="להתקשר לדניאל",
            due_at=now,
            source_event_id=event.id,
            status=CommitmentStatus.SCHEDULED,
            confidence=0.99,
            dedupe_key="retry-commitment",
        )
        session.add(commitment)
        await session.flush()
        session.add(
            Reminder(
                id=reminder_id,
                commitment_id=commitment.id,
                kind=ReminderKind.DUE,
                scheduled_for=now,
                status=ReminderStatus.PENDING,
                dedupe_key="retry-reminder",
            )
        )
        await session.commit()

    assert await service.dispatch_due_reminders(now) == 0
    async with factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        assert reminder is not None
        assert reminder.status is ReminderStatus.PENDING
        assert reminder.sent_at is None

    assert await service.dispatch_due_reminders(now) == 1
    async with factory() as session:
        reminder = await session.get(Reminder, reminder_id)
        assert reminder is not None
        assert reminder.status is ReminderStatus.SENT
        assert reminder.telegram_message_id == "message-1"
        follow_ups = list(
            (
                await session.scalars(select(Reminder).where(Reminder.kind == ReminderKind.SNOOZE))
            ).all()
        )
        assert len(follow_ups) == 1
        assert follow_ups[0].scheduled_for == now.replace(minute=15)
    await engine.dispose()
