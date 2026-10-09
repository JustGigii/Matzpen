import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from personal_agent.domain.database import Base, create_engine, create_session_factory
from personal_agent.domain.enums import (
    ApprovalStatus,
    EventDirection,
    EventSource,
    ProcessingStatus,
    Sensitivity,
)
from personal_agent.domain.models import ApprovalRequest, Event, Task
from personal_agent.integrations.telegram.runtime import TelegramRuntime


async def test_approval_and_reminder_time_pickers_offer_manual_entry(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'manual-button.db').as_posix()}")
    factory = create_session_factory(engine)
    approval_id = uuid.uuid4()
    task_id = uuid.uuid4()
    runtime = object.__new__(TelegramRuntime)
    runtime._session_factory = factory
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(
                ApprovalRequest(
                    id=approval_id,
                    action_type="clarify_commitment_time",
                    action_payload={"item": {}},
                    risk_class="internal_reversible",
                    status=ApprovalStatus.PENDING,
                )
            )
            session.add(
                Task(
                    id=task_id,
                    title="Manual reminder time",
                    due_at=datetime(2099, 8, 4, 16, 0, tzinfo=UTC),
                )
            )
            await session.commit()

        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        await runtime._show_approval_time_picker(query, approval_id)

        keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
        callback_values = {
            button.callback_data for row in keyboard.inline_keyboard for button in row
        }
        assert f"manualapproval:{approval_id}" in callback_values

        reminder_query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        await runtime._show_reminder_time_picker(reminder_query, task_id)
        reminder_keyboard = reminder_query.edit_message_text.await_args.kwargs["reply_markup"]
        reminder_callbacks = {
            button.callback_data for row in reminder_keyboard.inline_keyboard for button in row
        }
        assert f"manualitem:{task_id}" in reminder_callbacks
    finally:
        await engine.dispose()


async def test_replying_to_manual_time_prompt_resolves_the_original_approval(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'manual-time.db').as_posix()}")
    factory = create_session_factory(engine)
    approval_id = uuid.uuid4()
    runtime = object.__new__(TelegramRuntime)
    runtime._session_factory = factory
    runtime._timezone = ZoneInfo("Asia/Jerusalem")
    runtime._confirmation_service = SimpleNamespace(resolve_time=AsyncMock(return_value=True))
    runtime._reminder_service = None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(
                ApprovalRequest(
                    id=approval_id,
                    action_type="clarify_commitment_time",
                    action_payload={
                        "item": {
                            "due_at": None,
                            "explicit_date": "2099-08-04",
                        }
                    },
                    risk_class="internal_reversible",
                    status=ApprovalStatus.PENDING,
                )
            )
            session.add(
                Event(
                    source=EventSource.TELEGRAM,
                    source_account="777",
                    external_id="900",
                    event_type="manual_time.prompt",
                    direction=EventDirection.OUTBOUND,
                    occurred_at=datetime(2026, 8, 3, 16, 0, tzinfo=UTC),
                    received_at=datetime(2026, 8, 3, 16, 0, tzinfo=UTC),
                    conversation_external_id="123",
                    content_text="Manual time requested",
                    payload_json={"kind": "approval", "target_id": str(approval_id)},
                    dedupe_key="telegram:manual_time_prompt:900",
                    sensitivity=Sensitivity.PERSONAL,
                    processing_status=ProcessingStatus.PENDING,
                )
            )
            await session.commit()

        message = SimpleNamespace(
            message_id=901,
            text="19:30",
            reply_to_message=SimpleNamespace(message_id=900),
            reply_text=AsyncMock(),
        )

        assert await runtime._handle_manual_time_reply(message, "123") is True

        selected = runtime._confirmation_service.resolve_time.await_args.args[1]
        assert selected.isoformat() == "2099-08-04T19:30:00+03:00"
        assert "19:30" in message.reply_text.await_args.args[0]
        async with factory() as session:
            prompt = (
                await session.scalars(select(Event).where(Event.external_id == "900"))
            ).first()
            assert prompt is not None
            assert prompt.processing_status is ProcessingStatus.PROCESSED
    finally:
        await engine.dispose()


def test_manual_time_parser_accepts_tomorrow_and_rejects_invalid_clock() -> None:
    runtime = object.__new__(TelegramRuntime)
    runtime._timezone = ZoneInfo("Asia/Jerusalem")
    reference = datetime(2026, 8, 3, 16, 0, tzinfo=UTC)

    selected = runtime._parse_manual_datetime("\u05de\u05d7\u05e8 08:15", None, reference)

    assert selected.isoformat() == "2026-08-04T08:15:00+03:00"
    with pytest.raises(ValueError):
        runtime._parse_manual_datetime("29:99", None, reference)
