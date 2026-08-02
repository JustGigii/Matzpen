from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from personal_agent.core.config import Settings
from personal_agent.domain.schemas import ExtractionResult
from personal_agent.integrations.llm.fake import FakeLLMProvider
from personal_agent.integrations.telegram.fake import FakeTelegramNotifier
from personal_agent.main import create_app


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 7, 31, 10, 0, tzinfo=UTC)


@pytest.fixture
def app_factory(
    tmp_path: Path, fixed_now: datetime
) -> Callable[[list[ExtractionResult] | None], FastAPI]:
    counter = 0

    def factory(results: list[ExtractionResult] | None = None) -> FastAPI:
        nonlocal counter
        counter += 1
        database_path = (tmp_path / f"test-{counter}.db").as_posix()
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{database_path}",
            openwa_webhook_secret="test-webhook-secret",
            auto_create_schema=True,
            internal_action_grace_seconds=60,
            default_reminder_lead_minutes=5,
            gemini_api_key=None,
            gemini_model=None,
            telegram_bot_token=None,
            telegram_allowed_user_ids=(),
            shortcut_bearer_token="test-shortcut-token",
            google_client_secret_file=None,
            google_token_file=None,
        )
        return create_app(
            settings=settings,
            llm_provider=FakeLLMProvider(results),
            notifier=FakeTelegramNotifier(),
            clock=lambda: fixed_now,
        )

    return factory


@pytest.fixture
async def client_for_app() -> Callable[[FastAPI], AsyncIterator[AsyncClient]]:
    async def create_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                yield client

    return create_client
