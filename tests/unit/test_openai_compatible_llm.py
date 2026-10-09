from typing import Any

import httpx
import pytest

from personal_agent.domain.schemas import ExtractionResult
from personal_agent.integrations.llm.base import LLMQuotaExceededError
from personal_agent.integrations.llm.openai_compatible import (
    OpenAICompatibleProvider,
    strict_json_schema,
)


def assert_strict_objects(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            assert_strict_objects(item)
        return
    if not isinstance(value, dict):
        return
    properties = value.get("properties")
    if isinstance(properties, dict):
        assert value["required"] == list(properties)
        assert value["additionalProperties"] is False
    for child in value.values():
        assert_strict_objects(child)


def test_schema_is_adapted_for_strict_structured_output() -> None:
    schema = strict_json_schema(ExtractionResult)

    assert_strict_objects(schema)


def test_429_is_mapped_to_safe_quota_error() -> None:
    response = httpx.Response(
        429,
        headers={"retry-after": "12.2"},
        text="tokens per day limit reached",
        request=httpx.Request("POST", "https://provider.invalid/chat/completions"),
    )

    with pytest.raises(LLMQuotaExceededError) as raised:
        OpenAICompatibleProvider._validated_response(response)

    assert raised.value.retry_after_seconds == 13
    assert raised.value.daily_limit is True
    assert "tokens per day" not in str(raised.value)


def test_402_is_mapped_to_non_daily_quota_error() -> None:
    response = httpx.Response(
        402,
        text="payment or credits required",
        request=httpx.Request("POST", "https://provider.invalid/chat/completions"),
    )

    with pytest.raises(LLMQuotaExceededError) as raised:
        OpenAICompatibleProvider._validated_response(response)

    assert raised.value.retry_after_seconds == 3600
    assert raised.value.daily_limit is False
    assert "payment" not in str(raised.value)
