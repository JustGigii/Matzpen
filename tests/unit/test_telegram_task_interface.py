import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from personal_agent.domain.database import Base, create_engine, create_session_factory
from personal_agent.domain.enums import (
    ActionType,
    CommitmentDirection,
    CommitmentStatus,
    EventDirection,
    EventSource,
    ProcessingStatus,
    Sensitivity,
    TaskStatus,
)
from personal_agent.domain.models import Commitment, Event, Task
from personal_agent.integrations.telegram.runtime import TelegramRuntime


async def test_tasks_command_renders_only_open_tasks_as_actionable_cards(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'task-ui.db').as_posix()}")
    factory = create_session_factory(engine)
    open_task_id = uuid.uuid4()
    runtime = object.__new__(TelegramRuntime)
    runtime._allowed_user_ids = frozenset({123})
    runtime._session_factory = factory
    runtime._timezone = ZoneInfo("Asia/Jerusalem")
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=123),
        callback_query=None,
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all(
                [
                    Task(
                        id=open_task_id,
                        title="משימה פתוחה",
                        due_at=datetime(2099, 8, 4, 16, 0, tzinfo=UTC),
                    ),
                    Task(
                        title="משימה שכבר הושלמה",
                        status=TaskStatus.DONE,
                    ),
                ]
            )
            await session.commit()

        await runtime._tasks_command(update, SimpleNamespace())

        message.reply_text.assert_awaited_once()
        card = message.reply_text.await_args.args[0]
        keyboard = message.reply_text.await_args.kwargs["reply_markup"]
        callbacks = {button.callback_data for row in keyboard.inline_keyboard for button in row}
        assert "המשימות הפתוחות (1)" in card
        assert "משימה פתוחה" in card
        assert "משימה שכבר הושלמה" not in card
        assert "תעיף את 2" in card
        assert callbacks == {
            f"done:{open_task_id}",
            f"choose:{open_task_id}",
            f"drop:{open_task_id}",
        }
    finally:
        await engine.dispose()


async def test_empty_tasks_interface_has_no_legacy_bullet_list(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'empty-task-ui.db').as_posix()}")
    factory = create_session_factory(engine)
    runtime = object.__new__(TelegramRuntime)
    runtime._allowed_user_ids = frozenset({123})
    runtime._session_factory = factory
    runtime._timezone = ZoneInfo("Asia/Jerusalem")
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=123),
        callback_query=None,
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        await runtime._tasks_command(update, SimpleNamespace())

        message.reply_text.assert_awaited_once_with(
            "☑️ המשימות שלי\n━━━━━━━━━━━━\n✨ אין משימות פתוחות."
        )
    finally:
        await engine.dispose()


async def test_spoken_tasks_request_uses_the_same_actionable_interface() -> None:
    runtime = object.__new__(TelegramRuntime)
    runtime._render_task_cards = AsyncMock()
    update = SimpleNamespace(message=SimpleNamespace(text="מה המשימות שלי"))

    handled = await runtime._handle_spoken_request(update)

    assert handled is True
    runtime._render_task_cards.assert_awaited_once_with(update)


async def test_task_dashboard_callback_keeps_dashboard_and_removes_resolved_row(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'dashboard-callback.db').as_posix()}")
    factory = create_session_factory(engine)
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    runtime = object.__new__(TelegramRuntime)
    runtime._session_factory = factory
    runtime._primary_user_id = 123
    edit_message_text = AsyncMock()
    runtime._application = SimpleNamespace(bot=SimpleNamespace(edit_message_text=edit_message_text))
    edit_markup = AsyncMock()
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ 1", callback_data=f"done:{first_id}")],
            [InlineKeyboardButton("✅ 2", callback_data=f"done:{second_id}")],
        ]
    )
    query = SimpleNamespace(
        message=SimpleNamespace(
            message_id=77,
            text="☑️ המשימות הפתוחות (2)\n━━━━━━━━━━━━",
            reply_markup=markup,
        ),
        edit_message_reply_markup=edit_markup,
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all([Task(id=first_id, title="ראשונה"), Task(id=second_id, title="שנייה")])
            await session.commit()

        await runtime._synchronize_reminder_cards(
            query,
            "done",
            first_id,
            True,
            ["done", str(first_id)],
        )

        edit_message_text.assert_not_awaited()
        edit_markup.assert_awaited_once()
        remaining = edit_markup.await_args.kwargs["reply_markup"]
        callbacks = {button.callback_data for row in remaining.inline_keyboard for button in row}
        assert callbacks == {f"done:{second_id}"}
    finally:
        await engine.dispose()


async def test_delete_the_second_item_resolves_only_second_item_from_last_list(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'spoken-delete.db').as_posix()}")
    factory = create_session_factory(engine)
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    source_id = uuid.uuid4()
    now = datetime(2099, 8, 4, 16, 0, tzinfo=UTC)
    runtime = object.__new__(TelegramRuntime)
    runtime._session_factory = factory
    runtime._reminder_service = SimpleNamespace(
        cancel_commitment=AsyncMock(return_value=True),
        mark_done=AsyncMock(return_value=True),
    )
    message = SimpleNamespace(text="תמחק את השני", reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=123))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(
                Event(
                    id=source_id,
                    source=EventSource.TELEGRAM,
                    source_account="bot",
                    external_id="source",
                    event_type="message.received",
                    direction=EventDirection.INBOUND,
                    occurred_at=now,
                    received_at=now,
                    actor_external_id="123",
                    actor_display_name="User",
                    conversation_external_id="123",
                    content_text="מה יש",
                    payload_json={},
                    dedupe_key="source",
                    sensitivity=Sensitivity.PERSONAL,
                    processing_status=ProcessingStatus.PROCESSED,
                )
            )
            session.add_all(
                [
                    Commitment(
                        id=first_id,
                        direction=CommitmentDirection.USER_PROMISED,
                        action_type=ActionType.CALL,
                        summary="לקבוע שיחת ייעוץ עם האוניברסיטה הפתוחה",
                        source_event_id=source_id,
                        status=CommitmentStatus.SCHEDULED,
                        confidence=0.99,
                        dedupe_key="first",
                        created_at=now,
                    ),
                    Commitment(
                        id=second_id,
                        direction=CommitmentDirection.USER_PROMISED,
                        action_type=ActionType.MESSAGE,
                        summary="לשאול את נועה מה דעתה על השעון",
                        source_event_id=source_id,
                        status=CommitmentStatus.SCHEDULED,
                        confidence=0.99,
                        dedupe_key="second",
                        created_at=now + timedelta(seconds=1),
                    ),
                    Event(
                        source=EventSource.TELEGRAM,
                        source_account="bot",
                        external_id="assistant-list",
                        event_type="assistant.reply",
                        direction=EventDirection.OUTBOUND,
                        occurred_at=now + timedelta(seconds=2),
                        received_at=now + timedelta(seconds=2),
                        actor_external_id="agent",
                        actor_display_name="Agent",
                        conversation_external_id="123",
                        content_text=(
                            "ההתחייבויות שלך הן:\n"
                            "- לקבוע שיחת ייעוץ עם האוניברסיטה הפתוחה\n"
                            "- לשאול את נועה מה דעתה על השעון"
                        ),
                        payload_json={},
                        dedupe_key="assistant-list",
                        sensitivity=Sensitivity.PERSONAL,
                        processing_status=ProcessingStatus.PROCESSED,
                    ),
                ]
            )
            await session.commit()

        handled = await runtime._handle_spoken_request(update)

        assert handled is True
        runtime._reminder_service.cancel_commitment.assert_awaited_once_with(second_id)
        runtime._reminder_service.mark_done.assert_not_awaited()
        assert "לשאול את נועה" in message.reply_text.await_args.args[0]
        assert "שאר הרשימה לא השתנתה" in message.reply_text.await_args.args[0]

        async with factory() as session:
            second = await session.get(Commitment, second_id)
            assert second is not None
            second.status = CommitmentStatus.CANCELLED
            await session.commit()
        runtime._reminder_service.cancel_commitment.reset_mock()
        stale_message = SimpleNamespace(text="תמחק את השני", reply_text=AsyncMock())
        stale_update = SimpleNamespace(
            message=stale_message,
            effective_chat=SimpleNamespace(id=123),
        )

        assert await runtime._handle_spoken_request(stale_update) is True
        runtime._reminder_service.cancel_commitment.assert_not_awaited()
        assert "כבר בוטל" in stale_message.reply_text.await_args.args[0]
        assert "לא שיניתי פריטים אחרים" in stale_message.reply_text.await_args.args[0]
    finally:
        await engine.dispose()
