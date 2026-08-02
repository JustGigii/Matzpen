import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from typing import cast

from fastapi import FastAPI
from httpx import AsyncClient

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
        assert await briefs.trigger_fallback_if_due(fixed_now + timedelta(hours=2)) is False

        notifier = cast(FakeTelegramNotifier, app.state.notifier)
        brief_notifications = [item for item in notifier.notifications if item.kind == "text"]
        assert len(brief_notifications) == 1


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
