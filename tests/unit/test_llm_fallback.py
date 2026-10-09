from unittest.mock import AsyncMock

from personal_agent.domain.schemas import ChatRequest, ChatResponse
from personal_agent.integrations.llm.base import (
    LLMQuotaExceededError,
    LLMUnsupportedMediaError,
)
from personal_agent.integrations.llm.fallback import FallbackLLMProvider


def provider_with_chat(result: ChatResponse | Exception) -> AsyncMock:
    provider = AsyncMock()
    if isinstance(result, Exception):
        provider.chat.side_effect = result
    else:
        provider.chat.return_value = result
    return provider


async def test_quota_failure_falls_back_and_cooldown_skips_primary() -> None:
    primary = provider_with_chat(LLMQuotaExceededError(120, daily_limit=True))
    secondary = provider_with_chat(ChatResponse(reply="fallback worked"))
    provider = FallbackLLMProvider([("gemini", primary), ("groq", secondary)])
    request = ChatRequest(message="hello")

    first = await provider.chat(request)
    second = await provider.chat(request)

    assert first.reply == "fallback worked"
    assert second.reply == "fallback worked"
    primary.chat.assert_awaited_once_with(request)
    assert secondary.chat.await_count == 2


async def test_unsupported_media_is_skipped_without_a_cooldown() -> None:
    text_only = AsyncMock()
    text_only.extract_media_text.side_effect = LLMUnsupportedMediaError("application/pdf")
    ocr = AsyncMock()
    ocr.extract_media_text.return_value = "document text"
    provider = FallbackLLMProvider([("cerebras", text_only), ("mistral", ocr)])

    result = await provider.extract_media_text(b"pdf", "application/pdf", "document.pdf")

    assert result == "document text"
    text_only.extract_media_text.assert_awaited_once()
    ocr.extract_media_text.assert_awaited_once()


async def test_short_rate_limit_waits_and_retries_without_user_intervention() -> None:
    current_time = [0.0]
    waits: list[float] = []

    async def advance(seconds: float) -> None:
        waits.append(seconds)
        current_time[0] += seconds

    groq = AsyncMock()
    groq.chat.side_effect = [
        LLMQuotaExceededError(1),
        ChatResponse(reply="automatic retry worked"),
    ]
    provider = FallbackLLMProvider(
        [("groq", groq)],
        clock=lambda: current_time[0],
        sleep=advance,
        inline_retry_budget_seconds=5,
    )
    request = ChatRequest(message="hello")

    result = await provider.chat(request)

    assert result.reply == "automatic retry worked"
    assert waits == [1.1]
    assert groq.chat.await_count == 2


def test_daily_quota_uses_long_cooldown_even_with_short_retry_header() -> None:
    error = LLMQuotaExceededError(17, daily_limit=True)

    assert FallbackLLMProvider._cooldown_seconds(error) == 3600
