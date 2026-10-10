from collections.abc import Callable
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from personal_agent.domain.enums import EventDirection, EventSource
from personal_agent.domain.models import ApprovalRequest, Event, Task
from personal_agent.domain.schemas import NormalizedEvent
from personal_agent.services.intake import IntakeService


def test_telegram_context_question_is_not_treated_as_a_new_task() -> None:
    followup = Event(
        source=EventSource.TELEGRAM,
        content_text="מאיזה קבוצה? תן לי יותר פרטים",
    )
    real_task = Event(
        source=EventSource.TELEGRAM,
        content_text="תזכיר לי מחר לברר לגבי שיעור ההכנה",
    )

    assert IntakeService._is_context_followup(followup) is True
    assert IntakeService._is_context_followup(real_task) is False


def test_navigation_and_resolution_messages_are_not_new_items_from_any_source() -> None:
    for source, text in (
        (EventSource.TELEGRAM, "מה משימות שלי"),
        (EventSource.TELEGRAM, "משימה 1 בוצע"),
        (EventSource.WHATSAPP, "מה המשימות שלי?"),
        (EventSource.WHATSAPP, "סיימתי משימה 2"),
    ):
        event = Event(source=source, content_text=text)
        assert IntakeService._is_context_followup(event) is True


def test_misspelled_rewrite_followup_is_routed_to_chat() -> None:
    event = Event(
        source=EventSource.TELEGRAM,
        content_text="תנכל לכנתב את זה יותר יפה",
    )

    assert IntakeService._is_context_followup(event) is True


@pytest.mark.parametrize(
    "text",
    [
        "באופן כללי לקבוע פשוט להזכיר לי כל יום שאני יעשה את זה",
        "באופן כללי להזכיר לי כל יום עד שאני יעשה את זה",
        "תזכיר לי כל יום לעשות את זה",
        "תזכיר לי מחר לגבי זה",
    ],
)
def test_reminder_feedback_and_missing_subject_followups_are_not_new_items(text: str) -> None:
    event = Event(source=EventSource.TELEGRAM, content_text=text)

    assert IntakeService._is_context_followup(event) is True


@pytest.mark.parametrize(
    "text",
    [
        "תזכיר לי כל יום להתקשר לדני",
        "באופן כללי תזכיר לי כל יום לקחת ויטמינים",
    ],
)
def test_concrete_daily_reminder_requests_still_reach_extraction(text: str) -> None:
    event = Event(source=EventSource.TELEGRAM, content_text=text)

    assert IntakeService._is_context_followup(event) is False


async def test_reminder_behavior_feedback_is_saved_without_extracting_or_creating_work(
    app_factory: Callable[..., FastAPI],
    fixed_now: datetime,
) -> None:
    app = app_factory()
    async with app.router.lifespan_context(app):
        service = app.state.intake_service
        extract = AsyncMock(side_effect=AssertionError("Feedback must not reach extraction"))
        service._llm_provider.extract_event = extract

        result = await service.ingest(
            NormalizedEvent(
                source=EventSource.TELEGRAM,
                source_account="bot",
                external_id="feedback",
                event_type="message.received",
                direction=EventDirection.INBOUND,
                occurred_at=fixed_now,
                received_at=fixed_now,
                conversation_external_id="123",
                content_text="באופן כללי לקבוע פשוט להזכיר לי כל יום עד שאני יעשה את זה",
            )
        )

        assert result.created is True
        assert result.task_ids == result.commitment_ids == result.approval_ids == []
        extract.assert_not_awaited()
        async with app.state.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(Task)) == 0
            assert await session.scalar(select(func.count()).select_from(ApprovalRequest)) == 0
