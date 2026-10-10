import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from personal_agent.domain.database import Base, create_engine, create_session_factory
from personal_agent.domain.enums import (
    EventDirection,
    EventSource,
    ProcessingStatus,
    ReminderKind,
    ReminderStatus,
    Sensitivity,
    TaskStatus,
)
from personal_agent.domain.models import Event, Reminder, Task
from personal_agent.integrations.telegram.runtime import (
    TelegramRuntime,
    classify_item_resolution,
)
from personal_agent.services.reminders import ReminderService


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[TelegramRuntime]:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'resolution.db').as_posix()}")
    factory = create_session_factory(engine)
    instance = object.__new__(TelegramRuntime)
    instance._session_factory = factory
    instance._allowed_user_ids = frozenset({123})
    instance._timezone = ZoneInfo("Asia/Jerusalem")
    reminders = object.__new__(ReminderService)
    reminders._session_factory = factory
    reminders._lock = asyncio.Lock()
    reminders._now = lambda: datetime.now(UTC)
    instance._reminder_service = reminders
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield instance
    finally:
        await engine.dispose()


def _update(text: str, chat_id: int = 123) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=123),
    )


async def _tasks(runtime: TelegramRuntime, count: int = 2) -> list[uuid.UUID]:
    now = datetime.now(UTC)
    tasks = [
        Task(
            id=uuid.uuid4(),
            title=f"להתקשר לאיש קשר {index}",
            created_at=now + timedelta(seconds=index),
        )
        for index in range(count)
    ]
    async with runtime._session_factory() as session:
        session.add_all(tasks)
        await session.commit()
    return [task.id for task in tasks]


async def _legacy_reply(runtime: TelegramRuntime, content: str) -> None:
    now = datetime.now(UTC)
    reply_id = str(uuid.uuid4())
    async with runtime._session_factory() as session:
        session.add(
            Event(
                source=EventSource.TELEGRAM,
                source_account="test-bot",
                external_id=reply_id,
                event_type="assistant.reply",
                direction=EventDirection.OUTBOUND,
                occurred_at=now,
                received_at=now,
                conversation_external_id="123",
                content_text=content,
                payload_json={},
                dedupe_key=reply_id,
                sensitivity=Sensitivity.PERSONAL,
                processing_status=ProcessingStatus.PROCESSED,
            )
        )
        await session.commit()


async def _statuses(runtime: TelegramRuntime) -> dict[uuid.UUID, TaskStatus]:
    async with runtime._session_factory() as session:
        return {task.id: task.status for task in (await session.scalars(select(Task))).all()}


@pytest.mark.parametrize("text", ["תמחק הכל", "תעיף את כולם", "זה לא רלוונטי", "תוריד אותם"])
def test_bulk_cancellation_phrases_are_classified(text: str) -> None:
    resolution = classify_item_resolution(text)

    assert resolution is not None
    assert resolution.action == "cancel"
    assert resolution.all_visible is True


def test_qualified_bulk_request_is_not_mistaken_for_all_items() -> None:
    resolution = classify_item_resolution("תמחק הכל חוץ מהשני")

    assert resolution is not None
    assert resolution.all_visible is False


@pytest.mark.parametrize("text", ["תמחק הכל", "תעיף את כולם", "זה לא רלוונטי"])
async def test_bulk_cancellation_resolves_shown_items_only(
    runtime: TelegramRuntime,
    text: str,
) -> None:
    task_ids = await _tasks(runtime, 3)
    await runtime._remember_visible_items(_update(""), "task", task_ids[:2], "רשימה")
    async with runtime._session_factory() as session:
        session.add(
            Reminder(
                task_id=task_ids[0],
                kind=ReminderKind.DUE,
                scheduled_for=datetime.now(UTC),
                status=ReminderStatus.PENDING,
                dedupe_key="pending-reminder",
            )
        )
        await session.commit()
    update = _update(text)

    assert await runtime._handle_spoken_request(update) is True

    assert await _statuses(runtime) == {
        task_ids[0]: TaskStatus.CANCELLED,
        task_ids[1]: TaskStatus.CANCELLED,
        task_ids[2]: TaskStatus.PENDING,
    }
    async with runtime._session_factory() as session:
        reminder = await session.scalar(select(Reminder))
        assert reminder is not None and reminder.status == ReminderStatus.HANDLED
    assert "בוטלו 2 פריטים" in update.message.reply_text.await_args.args[0]


async def test_dashboard_persists_the_ten_shown_items_for_a_fresh_runtime(
    runtime: TelegramRuntime,
) -> None:
    task_ids = await _tasks(runtime, 11)
    await runtime._render_task_cards(_update("מה המשימות שלי"))
    restarted = object.__new__(TelegramRuntime)
    restarted._session_factory = runtime._session_factory
    restarted._reminder_service = runtime._reminder_service

    assert await restarted._handle_spoken_request(_update("תמחק הכל")) is True

    statuses = await _statuses(runtime)
    assert all(statuses[item_id] == TaskStatus.CANCELLED for item_id in task_ids[:10])
    assert statuses[task_ids[10]] == TaskStatus.PENDING


async def test_bulk_cancellation_without_shown_context_asks_for_a_list(
    runtime: TelegramRuntime,
) -> None:
    task_ids = await _tasks(runtime)
    update = _update("תמחק הכל")

    assert await runtime._handle_spoken_request(update) is True

    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.PENDING)
    assert "אין לי רשימה אחרונה מזוהה" in update.message.reply_text.await_args.args[0]


async def test_ordinals_never_fall_back_to_an_older_longer_list(runtime: TelegramRuntime) -> None:
    task_ids = await _tasks(runtime)
    await runtime._remember_visible_items(_update(""), "task", task_ids, "רשימה ישנה")
    await runtime._remember_visible_items(_update(""), "task", task_ids[:1], "רשימה חדשה")
    update = _update("תמחק את השני")

    assert await runtime._handle_spoken_request(update) is True

    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.PENDING)
    assert "לא ברור לי" in update.message.reply_text.await_args.args[0]


async def test_a_new_empty_list_does_not_resurrect_an_old_scope(runtime: TelegramRuntime) -> None:
    task_ids = await _tasks(runtime)
    await runtime._remember_visible_items(_update(""), "task", task_ids, "רשימה ישנה")
    await runtime._remember_visible_items(_update(""), "commitment", [], "אין התחייבויות")

    assert await runtime._handle_spoken_request(_update("תמחק הכל")) is True

    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.PENDING)


async def test_list_scope_is_specific_to_the_chat(runtime: TelegramRuntime) -> None:
    task_ids = await _tasks(runtime)
    await runtime._remember_visible_items(_update("", chat_id=999), "task", task_ids, "רשימה")

    assert await runtime._handle_spoken_request(_update("תמחק הכל")) is True

    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.PENDING)


async def test_legacy_chat_list_supports_bulk_cancellation(runtime: TelegramRuntime) -> None:
    task_ids = await _tasks(runtime)
    await _legacy_reply(runtime, "המשימות שלך:\n- להתקשר לאיש קשר 0\n- להתקשר לאיש קשר 1")

    assert await runtime._handle_spoken_request(_update("תעיף את כולם")) is True

    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.CANCELLED)


async def test_an_unrecognized_newer_list_does_not_use_older_context(
    runtime: TelegramRuntime,
) -> None:
    task_ids = await _tasks(runtime)
    await runtime._remember_visible_items(_update(""), "task", task_ids, "רשימה ישנה")
    await _legacy_reply(runtime, "הצעות:\n- לבוש נוח\n- חשיבה חיובית")

    assert await runtime._handle_spoken_request(_update("תמחק את השני")) is True

    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.PENDING)


async def test_a_pronoun_requires_a_single_unambiguous_shown_item(runtime: TelegramRuntime) -> None:
    task_ids = await _tasks(runtime)
    await runtime._remember_visible_items(_update(""), "task", task_ids, "רשימה")

    assert await runtime._handle_spoken_request(_update("תמחק את זה")) is True
    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.PENDING)

    await runtime._remember_visible_items(_update(""), "task", task_ids[:1], "פריט אחד")
    assert await runtime._handle_spoken_request(_update("תמחק את זה")) is True
    assert await _statuses(runtime) == {
        task_ids[0]: TaskStatus.CANCELLED,
        task_ids[1]: TaskStatus.PENDING,
    }
