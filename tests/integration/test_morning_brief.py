import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from personal_agent.domain.models import AuditLog, MorningBrief
from personal_agent.domain.schemas import CommitmentExtraction, ExtractionResult
from personal_agent.integrations.telegram.fake import FakeTelegramNotifier
from personal_agent.services.briefs import MorningBriefService
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


async def test_untimed_commitment_surfaces_daily_and_morning_trigger_deduplicates(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    extraction = ExtractionResult(
        language="he",
        items=[
            CommitmentExtraction(
                kind="commitment",
                summary="Send Daniel the file",
                action_type="send",
                confidence=0.98,
                evidence="send the file",
            )
        ],
    )
    app = app_factory([extraction])
    async for client in client_for_app(app):
        intake = await shortcut_post(client, "send the file", fixed_now)
        approval_id = cast(list[str], intake["approval_ids"])[0]
        reminders = cast(ReminderService, app.state.reminder_service)
        assert await reminders.execute_pending_action_now(uuid.UUID(approval_id)) is True

        first = await client.post(
            "/api/briefs/morning/trigger",
            headers={"Authorization": "Bearer test-shortcut-token"},
            json={"source": "waking_up"},
        )
        second = await client.post(
            "/api/briefs/morning/trigger",
            headers={"Authorization": "Bearer test-shortcut-token"},
            json={"source": "telegram_opened"},
        )
        assert first.status_code == second.status_code == 200
        assert first.json()["generated"] is True
        assert second.json()["generated"] is False
        assert first.json()["content"].count("Send Daniel the file") == 1
        assert second.json()["content"] == first.json()["content"]

        briefs = cast(MorningBriefService, app.state.morning_brief_service)
        later = await briefs.trigger(
            "test_next_day",
            send=False,
            at=fixed_now + timedelta(days=4),
        )
        assert later.content.count("Send Daniel the file") == 1
        assert await briefs.trigger_fallback_if_due(fixed_now + timedelta(hours=2)) is True
        assert await briefs.trigger_fallback_if_due(fixed_now + timedelta(hours=3)) is False

        notifier = cast(FakeTelegramNotifier, app.state.notifier)
        brief_notifications = [item for item in notifier.notifications if item.kind == "text"]
        assert len(brief_notifications) == 2


async def test_untimed_task_surfaces_daily_until_resolved(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    extraction = ExtractionResult(
        language="he",
        items=[
            CommitmentExtraction(
                kind="task",
                summary="Get a moving quote",
                action_type="other",
                confidence=0.98,
                evidence="get a moving quote",
            )
        ],
    )
    app = app_factory([extraction])
    async for client in client_for_app(app):
        intake = await shortcut_post(client, "get a moving quote", fixed_now)
        approval_id = cast(list[str], intake["approval_ids"])[0]
        reminders = cast(ReminderService, app.state.reminder_service)
        assert await reminders.execute_pending_action_now(uuid.UUID(approval_id)) is True

        briefs = cast(MorningBriefService, app.state.morning_brief_service)
        first = await briefs.trigger("test_day_one", send=False, at=fixed_now)
        later = await briefs.trigger(
            "test_next_day",
            send=False,
            at=fixed_now + timedelta(days=1),
        )

        assert first.content.count("Get a moving quote") == 1
        assert later.content.count("Get a moving quote") == 1

        task_id = cast(list[str], intake["task_ids"])[0]
        assert await reminders.mark_done(uuid.UUID(task_id)) is True
        after_completion = await briefs.trigger(
            "test_after_completion",
            send=False,
            at=fixed_now + timedelta(days=2),
        )
        assert "Get a moving quote" not in after_completion.content


async def test_morning_trigger_rejects_bad_token(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
) -> None:
    app = app_factory()
    async for client in client_for_app(app):
        response = await client.post(
            "/api/briefs/morning/trigger",
            headers={"Authorization": "Bearer wrong"},
            json={"source": "test"},
        )
        assert response.status_code == 401


async def test_ten_am_questions_survive_early_brief_and_service_restart(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    app = app_factory()
    async for _ in client_for_app(app):
        briefs = cast(MorningBriefService, app.state.morning_brief_service)
        early = fixed_now.replace(hour=5)  # 08:00 Jerusalem in July.
        assert (await briefs.trigger("waking_up", at=early)).sent
        assert await briefs.trigger_fallback_if_due(early) is False
        due = early + timedelta(hours=2)
        assert await briefs.trigger_fallback_if_due(due) is True
        restarted = MorningBriefService(
            app.state.session_factory,
            app.state.notifier,
            None,
            lambda at=due: at,
            "Asia/Jerusalem",
            time(10),
        )
        assert await restarted.trigger_fallback_if_due(due) is False
        assert await restarted.trigger_fallback_if_due(due + timedelta(days=1)) is True
        notifier = cast(FakeTelegramNotifier, app.state.notifier)
        assert len(notifier.notifications) == 3
        assert "אילו משימות או התחייבויות כבר ביצעת" in notifier.notifications[-1].text


async def test_brief_includes_past_future_and_undated_items_with_full_dates(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    from personal_agent.domain.models import Task

    app = app_factory()
    async for _ in client_for_app(app):
        async with app.state.session_factory() as session:
            session.add_all(
                [
                    Task(title="Past task", due_at=fixed_now - timedelta(days=1)),
                    Task(title="Future task", due_at=fixed_now + timedelta(days=4)),
                    Task(title="Undated task"),
                ]
            )
            await session.commit()
        brief = await app.state.morning_brief_service.trigger("test", send=False)
        for title in ["Past task", "Future task", "Undated task"]:
            assert brief.content.count(title) == 1
        assert "30.07.2026 13:00" in brief.content
        assert "04.08.2026 13:00" in brief.content
        assert "לפריטים ללא תאריך" in brief.content
        assert "הפריטים שבאיחור" in brief.content


async def test_failed_ten_am_delivery_retries_without_duplicate_success(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = app_factory()
    async for _ in client_for_app(app):
        delivery = AsyncMock(side_effect=[RuntimeError("offline"), "sent"])
        monkeypatch.setattr(app.state.notifier, "send_text", delivery)
        briefs = cast(MorningBriefService, app.state.morning_brief_service)
        with pytest.raises(RuntimeError, match="offline"):
            await briefs.trigger_fallback_if_due(fixed_now)
        async with app.state.session_factory() as session:
            stored = await session.scalar(select(MorningBrief))
            assert stored is not None and stored.sent_at is None
            assert (
                await session.scalar(
                    select(AuditLog.id).where(AuditLog.action == "daily_morning_check_in")
                )
                is None
            )
        assert await briefs.trigger_fallback_if_due(fixed_now) is True
        async with app.state.session_factory() as session:
            stored = await session.scalar(select(MorningBrief))
            assert stored is not None and stored.sent_at == fixed_now
        assert await briefs.trigger_fallback_if_due(fixed_now) is False
        assert delivery.await_count == 2


async def test_complete_ten_am_report_is_delivered_in_telegram_chunks(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personal_agent.domain.models import Task
    from personal_agent.integrations.telegram.runtime import TelegramRuntime

    app = app_factory()
    async for _ in client_for_app(app):
        async with app.state.session_factory() as session:
            session.add_all(
                [Task(title=f"משימה {index}: " + "פירוט ארוך " * 35) for index in range(30)]
            )
            await session.commit()
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
        runtime = TelegramRuntime.__new__(TelegramRuntime)
        runtime._application = SimpleNamespace(bot=bot)
        runtime._primary_user_id = 123
        monkeypatch.setattr(app.state.notifier, "send_text", runtime.send_text)
        briefs = cast(MorningBriefService, app.state.morning_brief_service)
        assert await briefs.trigger_fallback_if_due(fixed_now) is True
        chunks = [call.kwargs["text"] for call in bot.send_message.await_args_list]
        assert len(chunks) > 1
        assert all(len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks)
        async with app.state.session_factory() as session:
            stored = await session.scalar(select(MorningBrief))
            assert stored is not None
            assert "".join(chunks) == stored.content
            assert stored.sent_at == fixed_now
        assert "אילו משימות או התחייבויות כבר ביצעת" in chunks[-1]
        assert await briefs.trigger_fallback_if_due(fixed_now) is False
