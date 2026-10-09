from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from personal_agent.domain.database import Base, create_engine, create_session_factory
from personal_agent.domain.enums import (
    ActionType,
    ApprovalStatus,
    CalendarActionStatus,
    CommitmentDirection,
    CommitmentStatus,
    EventDirection,
    EventSource,
    ProcessingStatus,
    Sensitivity,
)
from personal_agent.domain.models import ApprovalRequest, CalendarAction, Commitment, Event
from personal_agent.integrations.google_calendar.fake import FakeCalendarProvider
from personal_agent.integrations.telegram.fake import FakeTelegramNotifier
from personal_agent.services.calendar import CalendarService


async def test_calendar_event_requires_explicit_approval_and_is_idempotent(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'calendar.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    provider = FakeCalendarProvider()
    notifier = FakeTelegramNotifier()
    now = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
    service = CalendarService(session_factory, provider, notifier, lambda: now)
    start = now + timedelta(days=1)
    end = start + timedelta(hours=1)

    approval_id = await service.propose_event("Planning", start, end)
    assert provider.events == []
    assert [notification.kind for notification in notifier.notifications] == ["approval_request"]

    assert await service.resolve_proposal(approval_id, approve=True) is True
    assert await service.resolve_proposal(approval_id, approve=True) is False
    assert len(provider.events) == 1

    async with session_factory() as session:
        approval = await session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
        )
        assert approval is not None
        assert approval.status is ApprovalStatus.EXECUTED
        assert approval.action_payload["google_event_id"] == provider.events[0].external_id
    await engine.dispose()


async def test_calendar_proposal_can_be_rejected_without_external_write(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'reject.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    provider = FakeCalendarProvider()
    service = CalendarService(
        session_factory,
        provider,
        FakeTelegramNotifier(),
        lambda: datetime(2026, 7, 31, 10, 0, tzinfo=UTC),
    )
    start = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    approval_id = await service.propose_event("Do not create", start, start + timedelta(hours=1))

    assert await service.resolve_proposal(approval_id, approve=False) is True
    assert provider.events == []
    await engine.dispose()


async def test_explicit_item_button_adds_timed_commitment_once(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'item.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    provider = FakeCalendarProvider()
    now = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
    due_at = now + timedelta(hours=2)
    service = CalendarService(
        session_factory,
        provider,
        FakeTelegramNotifier(),
        lambda: now,
    )
    async with session_factory() as session:
        event = Event(
            source=EventSource.TELEGRAM,
            source_account="bot",
            external_id="calendar-item-source",
            event_type="message.received",
            direction=EventDirection.INBOUND,
            occurred_at=now,
            received_at=now,
            actor_external_id="123",
            actor_display_name="User",
            conversation_external_id="123",
            content_text="תזכיר לי להתקשר לדניאל",
            payload_json={},
            dedupe_key="calendar-item-source",
            sensitivity=Sensitivity.PERSONAL,
            processing_status=ProcessingStatus.PROCESSED,
        )
        session.add(event)
        await session.flush()
        commitment = Commitment(
            direction=CommitmentDirection.USER_PROMISED,
            action_type=ActionType.CALL,
            summary="להתקשר לדניאל",
            due_at=due_at,
            source_event_id=event.id,
            status=CommitmentStatus.SCHEDULED,
            confidence=0.99,
            dedupe_key="calendar-item-commitment",
        )
        session.add(commitment)
        await session.commit()
        item_id = commitment.id

    assert await service.add_item_to_calendar(item_id) == "created"
    assert await service.add_item_to_calendar(item_id) == "already_exists"
    assert len(provider.events) == 1
    assert provider.events[0].summary == "להתקשר לדניאל"
    assert provider.events[0].start == due_at
    assert provider.events[0].end == due_at + timedelta(minutes=30)

    async with session_factory() as session:
        action = await session.scalar(select(CalendarAction))
        commitment = await session.get(Commitment, item_id)
        assert action is not None and action.status is CalendarActionStatus.EXECUTED
        assert commitment is not None and commitment.calendar_action_id == action.id
    await engine.dispose()
