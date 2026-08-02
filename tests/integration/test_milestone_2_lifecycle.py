import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.core.config import Settings
from personal_agent.domain.enums import (
    ApprovalStatus,
    CalendarActionStatus,
    CommitmentStatus,
    ReminderStatus,
)
from personal_agent.domain.models import (
    ApprovalRequest,
    CalendarAction,
    Commitment,
    Reminder,
    Task,
)
from personal_agent.domain.schemas import (
    CalendarProposal,
    CommitmentExtraction,
    ExtractionResult,
    TimeProposal,
    TimetableRow,
)
from personal_agent.integrations.google_calendar.fake import FakeCalendarProvider
from personal_agent.integrations.llm.fake import FakeLLMProvider
from personal_agent.integrations.telegram.fake import FakeTelegramNotifier
from personal_agent.integrations.telegram.runtime import TelegramRuntime
from personal_agent.main import create_app
from personal_agent.services.confirmations import ConfirmationService
from personal_agent.services.lifecycle import LifecycleService
from personal_agent.services.reminders import ReminderService


async def shortcut_post(
    client: AsyncClient, content: str, captured_at: datetime
) -> dict[str, object]:
    response = await client.post(
        "/api/intake/shortcut",
        headers={"Authorization": "Bearer test-shortcut-token"},
        json={
            "type": "text",
            "content": content,
            "captured_at": captured_at.isoformat(),
            "metadata": {"source": "test"},
        },
    )
    assert response.status_code == 200
    return cast(dict[str, object], response.json())


def milestone_app(
    tmp_path: Path,
    now: datetime,
    llm: FakeLLMProvider,
    *,
    calendar: FakeCalendarProvider | None = None,
) -> FastAPI:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / f'{uuid.uuid4()}.db').as_posix()}",
        openwa_webhook_secret="test-webhook-secret",
        auto_create_schema=True,
        shortcut_bearer_token="test-shortcut-token",
        internal_action_grace_seconds=60,
        proactive_check_interval_seconds=3600,
        gemini_api_key=None,
        gemini_model=None,
        telegram_bot_token=None,
        telegram_allowed_user_ids=(),
        google_client_secret_file=None,
        google_token_file=None,
    )
    return create_app(
        settings,
        llm_provider=llm,
        notifier=FakeTelegramNotifier(),
        calendar_provider=calendar,
        clock=lambda: now,
    )


async def test_low_confidence_approval_executes_full_workflow_once(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    extraction = ExtractionResult(
        language="he",
        items=[
            CommitmentExtraction(
                kind="commitment",
                summary="Call Daniel",
                action_type="call",
                due_at=fixed_now + timedelta(hours=2),
                confidence=0.70,
                evidence="call later",
            )
        ],
    )
    app = app_factory([extraction])
    async for client in client_for_app(app):
        body = await shortcut_post(client, "call later", fixed_now)
        assert body["commitment_ids"] == []
        approval_id = uuid.UUID(cast(list[str], body["approval_ids"])[0])
        confirmations = cast(ConfirmationService, app.state.confirmation_service)
        assert await confirmations.resolve(approval_id, approve=True) is True
        assert await confirmations.resolve(approval_id, approve=True) is False

        factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.status is ApprovalStatus.EXECUTED
            assert await session.scalar(select(func.count()).select_from(Commitment)) == 1
            assert await session.scalar(select(func.count()).select_from(Reminder)) == 2


async def test_ambiguous_time_uses_valid_fake_llm_fallback_once(
    tmp_path: Path,
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    fallback = fixed_now + timedelta(hours=6)
    llm = FakeLLMProvider(
        [
            ExtractionResult(
                language="he",
                items=[
                    CommitmentExtraction(
                        kind="commitment",
                        summary="Call Daniel this evening",
                        action_type="call",
                        confidence=0.82,
                        evidence="this evening",
                        ambiguous=True,
                        needs_clarification=True,
                        requires_user_confirmation=True,
                    )
                ],
            )
        ],
        [TimeProposal(proposed_at=fallback, decision_summary="Evening fallback")],
    )
    app = milestone_app(tmp_path, fixed_now, llm)
    async for client in client_for_app(app):
        body = await shortcut_post(client, "call Daniel this evening", fixed_now)
        approval_id = uuid.UUID(cast(list[str], body["approval_ids"])[0])
        reminders = cast(ReminderService, app.state.reminder_service)
        assert await reminders.resolve_due_clarifications(fixed_now + timedelta(minutes=9)) == 0
        assert await reminders.resolve_due_clarifications(fixed_now + timedelta(minutes=10)) == 1
        assert await reminders.resolve_due_clarifications(fixed_now + timedelta(minutes=11)) == 0

        factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with factory() as session:
            commitment = (await session.scalars(select(Commitment))).one()
            approval = await session.get(ApprovalRequest, approval_id)
            assert commitment.due_at == fallback
            assert commitment.resolution_source == "llm_fallback"
            assert approval is not None and approval.status is ApprovalStatus.EXECUTED
            assert await session.scalar(select(func.count()).select_from(Reminder)) == 2


async def test_smart_snooze_and_overdue_are_bounded_and_idempotent(
    tmp_path: Path,
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    due_at = fixed_now + timedelta(hours=1)
    snooze_at = due_at + timedelta(hours=1)
    llm = FakeLLMProvider(
        [
            ExtractionResult(
                language="en",
                items=[
                    CommitmentExtraction(
                        kind="commitment",
                        summary="Call Daniel",
                        action_type="call",
                        due_at=due_at,
                        confidence=0.99,
                        evidence="Call at eleven",
                    )
                ],
            )
        ],
        [TimeProposal(proposed_at=snooze_at, decision_summary="After current work")],
    )
    app = milestone_app(tmp_path, fixed_now, llm)
    async for client in client_for_app(app):
        body = await shortcut_post(client, "Call Daniel at eleven", fixed_now)
        approval_id = uuid.UUID(cast(list[str], body["approval_ids"])[0])
        service = cast(ReminderService, app.state.reminder_service)
        assert await service.execute_pending_action_now(approval_id) is True
        assert await service.dispatch_due_reminders(due_at - timedelta(minutes=5)) == 1

        factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with factory() as session:
            sent = (
                await session.scalars(
                    select(Reminder).where(Reminder.status == ReminderStatus.SENT)
                )
            ).one()
            reminder_id = sent.id
            commitment_id = sent.commitment_id
            assert sent.telegram_message_id is not None
            related_reminders = list(
                (
                    await session.scalars(
                        select(Reminder).where(Reminder.commitment_id == commitment_id)
                    )
                ).all()
            )
            for index, related_reminder in enumerate(related_reminders, start=101):
                related_reminder.telegram_message_id = str(index)
            await session.commit()
        assert await service.smart_snooze(reminder_id, due_at - timedelta(minutes=4)) is True
        assert await service.smart_snooze(reminder_id, due_at - timedelta(minutes=3)) is False
        assert await service.mark_overdue(due_at + timedelta(minutes=15)) == 1
        assert await service.mark_overdue(due_at + timedelta(minutes=30)) == 0

        async with factory() as session:
            commitment = await session.get(Commitment, commitment_id)
            assert commitment is not None
            assert commitment.status is CommitmentStatus.OVERDUE
            assert commitment.overdue_at is not None
            snoozes = list(
                (
                    await session.scalars(
                        select(Reminder).where(Reminder.status == ReminderStatus.PENDING)
                    )
                ).all()
            )
            assert len(snoozes) == 1
            assert sum(reminder.scheduled_for == snooze_at for reminder in snoozes) == 1

        edit_message_text = AsyncMock()
        runtime = object.__new__(TelegramRuntime)
        runtime._session_factory = factory
        runtime._primary_user_id = 123
        runtime._application = SimpleNamespace(
            bot=SimpleNamespace(edit_message_text=edit_message_text)
        )
        query = SimpleNamespace(message=SimpleNamespace(message_id=101))

        await runtime._synchronize_reminder_cards(
            query,
            "smart",
            reminder_id,
            True,
            ["smart", str(reminder_id)],
        )

        assert {call.kwargs["message_id"] for call in edit_message_text.await_args_list} == {
            101,
            102,
        }
        assert all(
            "הכפתורים בכרטיס הזה אינם פעילים עוד" in call.kwargs["text"]
            for call in edit_message_text.await_args_list
        )


async def test_calendar_meeting_and_selected_recurring_rows_are_idempotent(
    tmp_path: Path,
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    meeting_start = fixed_now + timedelta(days=1)
    calendar = FakeCalendarProvider()
    llm = FakeLLMProvider(
        [
            ExtractionResult(
                language="he",
                items=[
                    CommitmentExtraction(
                        kind="commitment",
                        summary="Meet Daniel",
                        action_type="meet",
                        due_at=meeting_start,
                        confidence=0.98,
                        evidence="meeting Sunday",
                        calendar_worthy=True,
                        calendar_event=CalendarProposal(
                            summary="Meeting with Daniel",
                            start=meeting_start,
                            end=meeting_start + timedelta(hours=1),
                        ),
                    )
                ],
            ),
            ExtractionResult(
                language="he",
                items=[
                    CommitmentExtraction(
                        kind="commitment",
                        summary="Course timetable",
                        action_type="other",
                        confidence=0.70,
                        evidence="two course rows",
                        requires_user_confirmation=True,
                        calendar_worthy=True,
                        timetable_rows=[
                            TimetableRow(
                                summary="Probability",
                                start=meeting_start + timedelta(days=1),
                                end=meeting_start + timedelta(days=1, hours=2),
                                recurrence=["RRULE:FREQ=WEEKLY;COUNT=12"],
                            ),
                            TimetableRow(
                                summary="Algorithms",
                                start=meeting_start + timedelta(days=2),
                                end=meeting_start + timedelta(days=2, hours=2),
                                recurrence=["RRULE:FREQ=WEEKLY;COUNT=12"],
                                included=False,
                            ),
                        ],
                    )
                ],
            ),
        ]
    )
    app = milestone_app(tmp_path, fixed_now, llm, calendar=calendar)
    async for client in client_for_app(app):
        first = await shortcut_post(client, "meeting Sunday", fixed_now)
        first_approval = uuid.UUID(cast(list[str], first["approval_ids"])[0])
        factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with factory() as session:
            action = (await session.scalars(select(CalendarAction))).one()
            assert action.status is CalendarActionStatus.PENDING
        service = cast(ReminderService, app.state.reminder_service)
        assert await service.execute_pending_action_now(first_approval) is True
        assert await service.execute_pending_action_now(first_approval) is False
        assert len(calendar.events) == 1

        second = await shortcut_post(client, "course timetable", fixed_now + timedelta(seconds=1))
        second_approval = uuid.UUID(cast(list[str], second["approval_ids"])[0])
        confirmations = cast(ConfirmationService, app.state.confirmation_service)
        assert await confirmations.resolve(second_approval, approve=True) is True
        assert await confirmations.resolve(second_approval, approve=True) is False
        assert len(calendar.events) == 2
        assert calendar.events[1].recurrence == ["RRULE:FREQ=WEEKLY;COUNT=12"]


async def test_approval_expires_without_recreating_the_question(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    extraction = ExtractionResult(
        language="en",
        items=[
            CommitmentExtraction(
                kind="commitment",
                summary="Maybe call Daniel",
                action_type="call",
                confidence=0.40,
                evidence="maybe",
                requires_user_confirmation=True,
            )
        ],
    )
    app = app_factory([extraction])
    async for client in client_for_app(app):
        first = await shortcut_post(client, "maybe", fixed_now)
        approval_id = uuid.UUID(cast(list[str], first["approval_ids"])[0])
        reminders = cast(ReminderService, app.state.reminder_service)
        assert await reminders.expire_approvals(fixed_now + timedelta(hours=24)) == 1
        duplicate = await shortcut_post(client, "maybe", fixed_now)
        assert duplicate["created"] is False
        confirmations = cast(ConfirmationService, app.state.confirmation_service)
        assert await confirmations.resolve(approval_id, approve=True) is False


async def test_assignment_task_gets_durable_reminders_and_calendar_action(
    tmp_path: Path,
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    deadline = fixed_now + timedelta(days=2)
    calendar = FakeCalendarProvider()
    llm = FakeLLMProvider(
        [
            ExtractionResult(
                language="en",
                items=[
                    CommitmentExtraction(
                        kind="task",
                        summary="Submit assignment 4",
                        action_type="send",
                        due_at=deadline,
                        confidence=0.95,
                        evidence="submit assignment 4 by the deadline",
                        assignment_deadline=True,
                        calendar_worthy=True,
                        calendar_event=CalendarProposal(
                            summary="Deadline: assignment 4",
                            start=deadline,
                            end=deadline + timedelta(minutes=15),
                        ),
                    )
                ],
            )
        ]
    )
    app = milestone_app(tmp_path, fixed_now, llm, calendar=calendar)
    async for client in client_for_app(app):
        body = await shortcut_post(client, "submit assignment 4", fixed_now)
        assert len(cast(list[str], body["task_ids"])) == 1
        approval_id = uuid.UUID(cast(list[str], body["approval_ids"])[0])
        service = cast(ReminderService, app.state.reminder_service)
        assert calendar.events == []
        assert await service.execute_pending_action_now(approval_id) is True
        factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(Task)) == 1
            task = (await session.scalars(select(Task))).one()
            task_reminders = list(
                (await session.scalars(select(Reminder).where(Reminder.task_id == task.id))).all()
            )
            assert {deadline - reminder.scheduled_for for reminder in task_reminders} == {
                timedelta(days=1),
                timedelta(hours=2),
            }
            assert await session.scalar(select(func.count()).select_from(CalendarAction)) == 1
        assert len(calendar.events) == 1


async def test_attendee_invitation_can_never_execute(
    tmp_path: Path,
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    start = fixed_now + timedelta(days=1)
    calendar = FakeCalendarProvider()
    llm = FakeLLMProvider(
        [
            ExtractionResult(
                language="en",
                items=[
                    CommitmentExtraction(
                        kind="commitment",
                        summary="Invite Daniel",
                        action_type="meet",
                        due_at=start,
                        confidence=0.99,
                        evidence="invite Daniel",
                        calendar_worthy=True,
                        calendar_event=CalendarProposal(
                            summary="Meeting",
                            start=start,
                            end=start + timedelta(hours=1),
                            attendees=["daniel@example.invalid"],
                        ),
                    )
                ],
            )
        ]
    )
    app = milestone_app(tmp_path, fixed_now, llm, calendar=calendar)
    async for client in client_for_app(app):
        body = await shortcut_post(client, "invite Daniel", fixed_now)
        approval_id = uuid.UUID(cast(list[str], body["approval_ids"])[0])
        confirmations = cast(ConfirmationService, app.state.confirmation_service)
        with pytest.raises(ValueError, match="Attendee invitations are not supported"):
            await confirmations.resolve(approval_id, approve=True)
        assert calendar.events == []
        factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None and approval.status is ApprovalStatus.FAILED


async def test_calendar_action_recovers_once_after_configuration(
    tmp_path: Path,
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    start = fixed_now + timedelta(days=1)
    llm = FakeLLMProvider(
        [
            ExtractionResult(
                language="en",
                items=[
                    CommitmentExtraction(
                        kind="commitment",
                        summary="Planning meeting",
                        action_type="meet",
                        due_at=start,
                        confidence=0.99,
                        evidence="planning meeting tomorrow",
                        calendar_worthy=True,
                        calendar_event=CalendarProposal(
                            summary="Planning meeting",
                            start=start,
                            end=start + timedelta(hours=1),
                        ),
                    )
                ],
            )
        ]
    )
    app = milestone_app(tmp_path, fixed_now, llm)
    async for client in client_for_app(app):
        body = await shortcut_post(client, "planning meeting tomorrow", fixed_now)
        approval_id = uuid.UUID(cast(list[str], body["approval_ids"])[0])
        service = cast(ReminderService, app.state.reminder_service)
        assert await service.execute_pending_action_now(approval_id) is True
        factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with factory() as session:
            action = (await session.scalars(select(CalendarAction))).one()
            assert action.status is CalendarActionStatus.PENDING_CONFIGURATION

        calendar = FakeCalendarProvider()
        recovery = LifecycleService(
            factory,
            cast(FakeTelegramNotifier, app.state.notifier),
            calendar,
            lambda: fixed_now,
            5,
        )
        assert await recovery.recover_configured_calendar_actions() == 1
        assert await recovery.recover_configured_calendar_actions() == 0
        assert len(calendar.events) == 1
