import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from personal_agent.domain.database import Base, create_engine, create_session_factory
from personal_agent.domain.enums import (
    ActionType,
    ApprovalStatus,
    CommitmentDirection,
    CommitmentStatus,
    EventDirection,
    EventSource,
    ProcessingStatus,
    ReminderKind,
    ReminderStatus,
    Sensitivity,
    TaskStatus,
)
from personal_agent.domain.models import ApprovalRequest, Commitment, Event, Reminder, Task
from personal_agent.integrations.telegram.runtime import (
    SpokenItemResolution,
    TelegramRuntime,
    classify_item_resolution,
    classify_spoken_intent,
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


def _update(text: str, chat_id: int = 123, user_id: int = 123) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        callback_query=None,
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


async def _commitments(runtime: TelegramRuntime, count: int = 2) -> list[uuid.UUID]:
    now = datetime.now(UTC)
    source_id = uuid.uuid4()
    source = Event(
        id=source_id,
        source=EventSource.TELEGRAM,
        source_account="test-bot",
        external_id=str(uuid.uuid4()),
        event_type="message.received",
        direction=EventDirection.INBOUND,
        occurred_at=now,
        received_at=now,
        conversation_external_id="123",
        payload_json={},
        dedupe_key=str(uuid.uuid4()),
        sensitivity=Sensitivity.PERSONAL,
        processing_status=ProcessingStatus.PROCESSED,
    )
    commitments = [
        Commitment(
            direction=CommitmentDirection.USER_PROMISED,
            action_type=ActionType.OTHER,
            summary=f"התחייבות פתוחה {index}",
            source_event_id=source_id,
            status=CommitmentStatus.SCHEDULED,
            confidence=1.0,
            dedupe_key=str(uuid.uuid4()),
            created_at=now + timedelta(seconds=index),
        )
        for index in range(count)
    ]
    async with runtime._session_factory() as session:
        session.add(source)
        session.add_all(commitments)
        await session.commit()
    return [commitment.id for commitment in commitments]


async def _pending_approval(
    runtime: TelegramRuntime,
    action_type: str,
    summary: str,
    **item_fields: object,
) -> uuid.UUID:
    approval = ApprovalRequest(
        id=uuid.uuid4(),
        action_type=action_type,
        action_payload={"item": {"summary": summary, **item_fields}},
        risk_class="internal_reversible",
        status=ApprovalStatus.PENDING,
        dedupe_key=f"pending-test:{uuid.uuid4()}",
    )
    async with runtime._session_factory() as session:
        session.add(approval)
        await session.commit()
    return approval.id


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("מה ממתין לאישור?", "pending_approvals"),
        ("ולהבהרה", "pending_clarifications"),
        ("מה צריך להבהיר?", "pending_clarifications"),
    ],
)
def test_pending_approval_and_clarification_queries_route_deterministically(
    text: str,
    intent: str,
) -> None:
    assert classify_spoken_intent(text) == intent


async def test_pending_approvals_are_read_from_database_and_listed_with_actions(
    runtime: TelegramRuntime,
) -> None:
    approval_id = await _pending_approval(
        runtime,
        "confirm_extraction",
        "לדבר עם דניאל מחר",
    )
    update = _update("מה ממתין לאישור?")

    assert await runtime._handle_spoken_request(update) is True

    update.message.reply_text.assert_awaited_once()
    assert "לדבר עם דניאל מחר" in update.message.reply_text.await_args.args[0]
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == f"approve:{approval_id}"


async def test_pending_clarification_query_includes_question_and_choices(
    runtime: TelegramRuntime,
) -> None:
    await _pending_approval(
        runtime,
        "clarify_details",
        "לקנות כרטיס",
        clarification_question="לאיזה אירוע לקנות כרטיס?",
        clarification_options=["הופעה", "משחק"],
    )
    update = _update("ולהבהרה")

    assert await runtime._handle_spoken_request(update) is True

    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    assert "לקנות כרטיס" in reply
    assert "לאיזה אירוע לקנות כרטיס?" in reply
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "הופעה"


async def test_pending_clarifications_exclude_resolved_approvals(runtime: TelegramRuntime) -> None:
    await _pending_approval(runtime, "clarify_extraction", "לבחור שעה")
    completed = ApprovalRequest(
        action_type="clarify_details",
        action_payload={"item": {"summary": "בקשה שכבר נפתרה"}},
        risk_class="internal_reversible",
        status=ApprovalStatus.EXECUTED,
        dedupe_key="resolved-test",
    )
    async with runtime._session_factory() as session:
        session.add(completed)
        await session.commit()
    update = _update("מה ממתין להבהרה?")

    assert await runtime._handle_spoken_request(update) is True

    assert "לבחור שעה" in update.message.reply_text.await_args.args[0]
    assert "בקשה שכבר נפתרה" not in update.message.reply_text.await_args.args[0]


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
    if text == "תמחק הכל":
        assert resolution.all_open is True
    else:
        assert resolution.all_visible is True


@pytest.mark.parametrize("text", ["תמחק הכל", "תציג לי ואז תמחק אותם"])
def test_explicit_delete_all_phrases_target_every_open_item(text: str) -> None:
    resolution = classify_item_resolution(text)

    assert resolution == SpokenItemResolution(action="cancel", all_open=True)


def test_qualified_bulk_request_is_not_mistaken_for_all_items() -> None:
    resolution = classify_item_resolution("תמחק הכל חוץ מהשני")

    assert resolution is not None
    assert resolution.all_visible is False


@pytest.mark.parametrize("text", ["תעיף את כולם", "זה לא רלוונטי"])
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


async def test_dashboard_persists_all_shown_items_for_a_fresh_runtime(
    runtime: TelegramRuntime,
) -> None:
    task_ids = await _tasks(runtime, 11)
    await runtime._render_task_cards(_update("מה המשימות שלי"))
    restarted = object.__new__(TelegramRuntime)
    restarted._session_factory = runtime._session_factory
    restarted._reminder_service = runtime._reminder_service

    assert await restarted._handle_spoken_request(_update("תמחק הכל")) is True

    statuses = await _statuses(runtime)
    assert all(statuses[item_id] == TaskStatus.CANCELLED for item_id in task_ids)


async def test_contextual_bulk_cancellation_without_shown_context_asks_for_a_list(
    runtime: TelegramRuntime,
) -> None:
    task_ids = await _tasks(runtime)
    update = _update("תעיף את כולם")

    assert await runtime._handle_spoken_request(update) is True

    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.PENDING)
    assert "אין לי רשימה אחרונה מזוהה" in update.message.reply_text.await_args.args[0]


@pytest.mark.parametrize("text", ["תמחק הכל", "תציג לי ואז תמחק אותם"])
async def test_explicit_delete_all_cancels_all_tasks_commitments_and_reminders(
    runtime: TelegramRuntime,
    text: str,
) -> None:
    task_ids = await _tasks(runtime, 2)
    commitment_ids = await _commitments(runtime, 2)
    async with runtime._session_factory() as session:
        session.add_all(
            [
                Reminder(
                    task_id=task_ids[0],
                    kind=ReminderKind.DUE,
                    scheduled_for=datetime.now(UTC),
                    status=ReminderStatus.PENDING,
                    dedupe_key=f"bulk-task-{text}",
                ),
                Reminder(
                    commitment_id=commitment_ids[0],
                    kind=ReminderKind.DUE,
                    scheduled_for=datetime.now(UTC),
                    status=ReminderStatus.SENT,
                    dedupe_key=f"bulk-commitment-{text}",
                ),
            ]
        )
        await session.commit()
    update = _update(text)

    assert await runtime._handle_spoken_request(update) is True

    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.CANCELLED)
    async with runtime._session_factory() as session:
        commitments = (await session.scalars(select(Commitment))).all()
        reminders = (await session.scalars(select(Reminder))).all()
    assert {item.id: item.status for item in commitments} == dict.fromkeys(
        commitment_ids, CommitmentStatus.CANCELLED
    )
    assert {reminder.status for reminder in reminders} == {ReminderStatus.HANDLED}
    replies = "\n".join(call.args[0] for call in update.message.reply_text.await_args_list)
    assert "בוטלו 4 פריטים פתוחים" in replies
    assert all(f"להתקשר לאיש קשר {index}" in replies for index in range(2))
    assert all(f"התחייבות פתוחה {index}" in replies for index in range(2))
    assert all(
        "reply_markup" not in call.kwargs for call in update.message.reply_text.await_args_list
    )


async def test_explicit_delete_all_reports_empty_state(runtime: TelegramRuntime) -> None:
    update = _update("תמחק הכל")

    assert await runtime._handle_spoken_request(update) is True

    update.message.reply_text.assert_awaited_once_with("✨ אין משימות או התחייבויות פתוחות למחיקה.")


async def test_explicit_delete_all_lists_every_title_across_telegram_chunks(
    runtime: TelegramRuntime,
) -> None:
    task_ids = await _tasks(runtime, 220)
    update = _update("תמחק הכל")

    assert await runtime._handle_spoken_request(update) is True

    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert len(replies) > 1
    assert all(len(reply.encode("utf-16-le")) // 2 <= 3900 for reply in replies)
    combined = "".join(replies)
    assert all(f"• להתקשר לאיש קשר {index}" in combined for index in range(220))
    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.CANCELLED)


async def test_spoken_ci_cd_request_sends_fixed_document(runtime: TelegramRuntime) -> None:
    bot = SimpleNamespace()
    runtime._application = SimpleNamespace(bot=bot)
    update = _update("שלח לי את מסמך ה-CI/CD")
    sender = AsyncMock(return_value="42")

    with patch("personal_agent.integrations.telegram.runtime.send_ci_cd_document", sender):
        assert await runtime._handle_spoken_request(update) is True

    sender.assert_awaited_once_with(bot, 123, Path.cwd())
    update.message.reply_text.assert_not_awaited()


async def test_spoken_ci_cd_request_rejects_unauthorized_user(
    runtime: TelegramRuntime,
) -> None:
    update = _update("שלח לי את מסמך ה-CI/CD", user_id=999)
    sender = AsyncMock()

    with patch("personal_agent.integrations.telegram.runtime.send_ci_cd_document", sender):
        assert await runtime._handle_spoken_request(update) is True

    sender.assert_not_awaited()
    update.message.reply_text.assert_awaited_once_with("אין הרשאה להשתמש בסוכן הזה.")


@pytest.mark.parametrize(
    "text",
    [
        "אל תמחק הכל",
        "תמחק הכל חוץ מהשני",
        "אולי תמחק הכל",
        "תציג לי הכל",
        "תמחק אותם",
        "תמחק את כל המשימות",
    ],
)
def test_non_exact_or_negated_phrases_do_not_target_every_open_item(text: str) -> None:
    resolution = classify_item_resolution(text)

    assert resolution is None or resolution.all_open is False


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

    assert await runtime._handle_spoken_request(_update("תעיף את כולם")) is True

    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.PENDING)


async def test_list_scope_is_specific_to_the_chat(runtime: TelegramRuntime) -> None:
    task_ids = await _tasks(runtime)
    await runtime._remember_visible_items(_update("", chat_id=999), "task", task_ids, "רשימה")

    assert await runtime._handle_spoken_request(_update("תעיף את כולם")) is True

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


async def test_bulk_done_requires_confirmation_and_keeps_snapshot_scope(
    runtime: TelegramRuntime,
) -> None:
    task_ids = await _tasks(runtime, 25)
    request = _update("סגור הכל סיימתי")
    assert await runtime._handle_spoken_request(request) is True
    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.PENDING)
    markup = request.message.reply_text.await_args.kwargs["reply_markup"]
    data = markup.inline_keyboard[0][0].callback_data
    new_ids = await _tasks(runtime, 1)
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
    callback = _update("")
    callback.callback_query = query
    await runtime._callback(callback, SimpleNamespace())
    statuses = await _statuses(runtime)
    assert all(statuses[item_id] == TaskStatus.DONE for item_id in task_ids)
    assert statuses[new_ids[0]] == TaskStatus.PENDING
    query.edit_message_text.reset_mock()
    await runtime._callback(callback, SimpleNamespace())
    query.edit_message_text.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_bulk_done_confirmation_is_bound_to_the_requesting_chat(
    runtime: TelegramRuntime,
) -> None:
    task_ids = await _tasks(runtime, 2)
    request = _update("סגור הכל סיימתי")
    assert await runtime._handle_spoken_request(request) is True
    markup = request.message.reply_text.await_args.kwargs["reply_markup"]
    data = markup.inline_keyboard[0][0].callback_data
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
    callback = _update("", chat_id=999)
    callback.callback_query = query

    await runtime._callback(callback, SimpleNamespace())

    assert await _statuses(runtime) == dict.fromkeys(task_ids, TaskStatus.PENDING)
    query.edit_message_text.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_mixed_overview_shows_and_orders_past_future_and_untimed_items(
    runtime: TelegramRuntime,
) -> None:
    now = datetime(2026, 10, 10, 7, 0, tzinfo=UTC)
    source_id = uuid.uuid4()
    source = Event(
        id=source_id,
        source=EventSource.TELEGRAM,
        source_account="test-bot",
        external_id="mixed-overview-source",
        event_type="message.received",
        direction=EventDirection.INBOUND,
        occurred_at=now,
        received_at=now,
        conversation_external_id="123",
        payload_json={},
        dedupe_key="mixed-overview-source",
        sensitivity=Sensitivity.PERSONAL,
        processing_status=ProcessingStatus.PROCESSED,
    )
    past_task = Task(title="משימת עבר", due_at=now - timedelta(days=1), created_at=now)
    future_commitment = Commitment(
        direction=CommitmentDirection.USER_PROMISED,
        action_type=ActionType.OTHER,
        summary="התחייבות עתידית",
        due_at=now + timedelta(days=1),
        source_event_id=source_id,
        status=CommitmentStatus.SCHEDULED,
        confidence=1.0,
        dedupe_key="future-mixed-commitment",
        created_at=now,
    )
    untimed_task = Task(title="משימה ללא מועד", created_at=now)
    untimed_commitment = Commitment(
        direction=CommitmentDirection.USER_PROMISED,
        action_type=ActionType.OTHER,
        summary="התחייבות ללא מועד",
        source_event_id=source_id,
        status=CommitmentStatus.SCHEDULED,
        confidence=1.0,
        dedupe_key="untimed-mixed-commitment",
        created_at=now + timedelta(seconds=1),
    )
    async with runtime._session_factory() as session:
        session.add_all([source, past_task, future_commitment, untimed_task, untimed_commitment])
        await session.commit()

    update = _update("תראה לי הכל")
    assert await runtime._handle_spoken_request(update) is True
    combined = "\n".join(call.args[0] for call in update.message.reply_text.await_args_list)

    assert combined.index("1. משימה: משימת עבר") < combined.index("2. התחייבות: התחייבות עתידית")
    assert "3. משימה: משימה ללא מועד" in combined
    assert "4. התחייבות: התחייבות ללא מועד" in combined
    assert combined.count("ללא מועד — צריך לקבוע תאריך") == 2

    followup = _update("סיימתי את 2")
    assert await runtime._handle_spoken_request(followup) is True
    async with runtime._session_factory() as session:
        refreshed = await session.get(Commitment, future_commitment.id)
        assert refreshed is not None and refreshed.status is CommitmentStatus.DONE


async def test_all_dashboard_keeps_all_items_and_large_number_references(
    runtime: TelegramRuntime,
) -> None:
    task_ids = await _tasks(runtime, 45)
    update = _update("תראה לי הכל")
    assert await runtime._handle_spoken_request(update) is True
    texts = [call.args[0] for call in update.message.reply_text.await_args_list]
    combined = "\n".join(texts)
    assert "(45)" in combined
    assert "45. משימה:" in combined
    assert "ללא מועד" in combined
    assert len(texts) >= 3
    assert all(len(text.encode("utf-16-le")) // 2 <= 4096 for text in texts)
    followup = _update("סיימתי את 45")
    assert await runtime._handle_spoken_request(followup) is True
    statuses = await _statuses(runtime)
    assert statuses[task_ids[-1]] == TaskStatus.DONE
    assert all(statuses[item_id] == TaskStatus.PENDING for item_id in task_ids[:-1])


async def test_calendar_text_explicit_all_uses_displayed_scope(runtime: TelegramRuntime) -> None:
    task_ids = await _tasks(runtime, 3)
    await runtime._render_task_cards(_update("מה המשימות שלי"))
    runtime._calendar_service = SimpleNamespace(
        add_items_to_calendar=AsyncMock(return_value=dict.fromkeys(task_ids, "untimed"))
    )
    update = _update("תוסיף הכל ליומן")
    assert await runtime._handle_spoken_request(update) is True
    runtime._calendar_service.add_items_to_calendar.assert_awaited_once_with(
        task_ids, approval_source="explicit_telegram_text"
    )
    assert "צריך תאריך ושעה" in update.message.reply_text.await_args.args[0]


async def test_calendar_text_without_all_does_not_guess_multiple_items(
    runtime: TelegramRuntime,
) -> None:
    await _tasks(runtime, 2)
    await runtime._render_task_cards(_update("מה המשימות שלי"))
    runtime._calendar_service = SimpleNamespace(add_items_to_calendar=AsyncMock())
    update = _update("תוסיף לי ביומן")
    assert await runtime._handle_spoken_request(update) is True
    runtime._calendar_service.add_items_to_calendar.assert_not_awaited()
    assert "איזה פריט" in update.message.reply_text.await_args.args[0]


def test_telegram_chunking_preserves_long_unicode_text() -> None:
    from personal_agent.integrations.telegram.text import split_telegram_text

    text = "משימות ✅📅\n" * 1500
    chunks = split_telegram_text(text)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3900 for chunk in chunks)
    with pytest.raises(ValueError):
        split_telegram_text("📅", limit=1)
