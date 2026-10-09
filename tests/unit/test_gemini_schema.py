from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from google.genai import errors

from personal_agent.domain.schemas import ChatRequest, ExtractionResult, TimeProposal
from personal_agent.integrations.llm.base import LLMServiceUnavailableError
from personal_agent.integrations.llm.gemini import (
    SERVING_SCHEMA_CONSTRAINTS,
    GeminiProvider,
    is_schema_state_error,
    quota_exceeded_error,
    relaxed_serving_schema,
    service_unavailable_error,
)


def collect_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(collect_keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(collect_keys(child) for child in value))
    return set()


def test_gemini_serving_schemas_remove_state_heavy_constraints() -> None:
    extraction_schema = relaxed_serving_schema(ExtractionResult)
    time_schema = relaxed_serving_schema(TimeProposal)

    assert SERVING_SCHEMA_CONSTRAINTS.isdisjoint(collect_keys(extraction_schema))
    assert SERVING_SCHEMA_CONSTRAINTS.isdisjoint(collect_keys(time_schema))
    assert extraction_schema["properties"]["items"]["type"] == "array"
    assert "$defs" in extraction_schema


def test_only_the_specific_schema_complexity_error_uses_the_json_fallback() -> None:
    complexity = errors.ClientError(
        400,
        {"error": {"message": "The specified schema has too many states"}},
        None,
    )
    other_bad_request = errors.ClientError(
        400,
        {"error": {"message": "Another invalid argument"}},
        None,
    )

    assert is_schema_state_error(complexity) is True
    assert is_schema_state_error(other_bad_request) is False


def test_quota_error_exposes_safe_retry_metadata() -> None:
    provider_error = errors.ClientError(
        429,
        {
            "error": {
                "message": "Quota exceeded. Please retry in 39.570841035s.",
                "details": [
                    {
                        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                    }
                ],
            }
        },
        None,
    )

    quota_error = quota_exceeded_error(provider_error)

    assert quota_error.retry_after_seconds == 40
    assert quota_error.daily_limit is True
    assert "Quota exceeded" not in str(quota_error)


def test_service_unavailable_error_hides_provider_details_and_can_be_retried() -> None:
    provider_error = errors.ServerError(
        503,
        {
            "error": {
                "message": "This model is currently experiencing high demand.",
                "status": "UNAVAILABLE",
            }
        },
        None,
    )

    unavailable = service_unavailable_error(provider_error)

    assert unavailable.retry_after_seconds == 30
    assert "high demand" not in str(unavailable)


async def test_chat_maps_final_503_to_safe_retryable_error() -> None:
    provider_error = errors.ServerError(
        503,
        {"error": {"message": "high demand", "status": "UNAVAILABLE"}},
        None,
    )
    provider = object.__new__(GeminiProvider)
    provider._client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=AsyncMock(side_effect=provider_error))
        )
    )
    provider._model = "fake-model"
    provider._max_attempts = 1

    with pytest.raises(LLMServiceUnavailableError, match="temporarily unavailable"):
        await provider.chat(ChatRequest(message="שלום"))
