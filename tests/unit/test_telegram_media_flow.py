from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from personal_agent.domain.schemas import IntakeResult
from personal_agent.integrations.telegram.runtime import TelegramRuntime


class _SessionContext:
    def __init__(self) -> None:
        self.session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    async def __aenter__(self) -> SimpleNamespace:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_media_text_enters_intake_without_intermediate_messages() -> None:
    runtime = object.__new__(TelegramRuntime)
    runtime._allowed_user_ids = frozenset({123})
    runtime._control = SimpleNamespace(paused=False)
    runtime._max_media_bytes = 18_000_000
    runtime._session_factory = _SessionContext
    runtime._application = SimpleNamespace(bot=SimpleNamespace(id=777))
    runtime._media_text_service = SimpleNamespace(
        extract_text=AsyncMock(return_value="קבע פגישה עם יואל מחר ב־17:00")
    )
    runtime._intake_service = SimpleNamespace(
        ingest=AsyncMock(
            return_value=IntakeResult(
                event_id="event-1",
                created=True,
                approval_ids=["approval-1"],
            )
        )
    )
    runtime._handle_spoken_request = AsyncMock(return_value=False)

    message = SimpleNamespace(
        message_id=55,
        date=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        voice=SimpleNamespace(
            file_id="file-1",
            file_unique_id="unique-1",
            mime_type="audio/ogg",
            file_size=100,
        ),
        audio=None,
        document=None,
        photo=None,
        caption=None,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=123, full_name="Test User"),
        effective_chat=SimpleNamespace(id=123),
    )
    telegram_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"ogg")))
    context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=telegram_file)))

    await runtime._ingest_media(update, context)

    message.reply_text.assert_not_awaited()
    runtime._intake_service.ingest.assert_awaited_once()
    candidate = runtime._intake_service.ingest.await_args.args[0]
    assert candidate.content_text == "קבע פגישה עם יואל מחר ב־17:00"
    assert candidate.event_type == "voice.received"


@pytest.mark.asyncio
async def test_media_without_an_action_gets_one_helpful_reply() -> None:
    runtime = object.__new__(TelegramRuntime)
    runtime._allowed_user_ids = frozenset({123})
    runtime._control = SimpleNamespace(paused=False)
    runtime._max_media_bytes = 18_000_000
    runtime._session_factory = _SessionContext
    runtime._application = SimpleNamespace(bot=SimpleNamespace(id=777))
    runtime._media_text_service = SimpleNamespace(extract_text=AsyncMock(return_value="שלום"))
    runtime._intake_service = SimpleNamespace(
        ingest=AsyncMock(return_value=IntakeResult(event_id="event-2", created=True))
    )
    runtime._handle_spoken_request = AsyncMock(return_value=False)

    message = SimpleNamespace(
        message_id=56,
        date=datetime(2026, 8, 1, 12, 1, tzinfo=UTC),
        voice=SimpleNamespace(
            file_id="file-2",
            file_unique_id="unique-2",
            mime_type="audio/ogg",
            file_size=100,
        ),
        audio=None,
        document=None,
        photo=None,
        caption=None,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=123, full_name="Test User"),
        effective_chat=SimpleNamespace(id=123),
    )
    telegram_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"ogg")))
    context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=telegram_file)))

    await runtime._ingest_media(update, context)

    message.reply_text.assert_awaited_once()
    assert "לא מצאתי" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_text_enters_intake_without_a_technical_summary() -> None:
    runtime = object.__new__(TelegramRuntime)
    runtime._allowed_user_ids = frozenset({123})
    runtime._control = SimpleNamespace(paused=False)
    runtime._application = SimpleNamespace(bot=SimpleNamespace(id=777))
    runtime._intake_service = SimpleNamespace(
        ingest=AsyncMock(
            return_value=IntakeResult(
                event_id="event-3",
                created=True,
                approval_ids=["approval-3"],
            )
        )
    )
    runtime._handle_spoken_request = AsyncMock(return_value=False)

    message = SimpleNamespace(
        message_id=57,
        date=datetime(2026, 8, 1, 12, 2, tzinfo=UTC),
        text="קבע פגישה עם יואל מחר ב־17:00",
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=123, full_name="Test User"),
        effective_chat=SimpleNamespace(id=123),
    )

    await runtime._ingest_text(update, SimpleNamespace())

    message.reply_text.assert_not_awaited()
    runtime._intake_service.ingest.assert_awaited_once()
