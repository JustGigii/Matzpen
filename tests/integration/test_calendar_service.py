from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from personal_agent.domain.database import Base, create_engine, create_session_factory
from personal_agent.domain.enums import ApprovalStatus
from personal_agent.domain.models import ApprovalRequest
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
