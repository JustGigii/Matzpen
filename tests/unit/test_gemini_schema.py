from typing import Any

from google.genai import errors

from personal_agent.domain.schemas import ExtractionResult, TimeProposal
from personal_agent.integrations.llm.gemini import (
    SERVING_SCHEMA_CONSTRAINTS,
    is_schema_state_error,
    relaxed_serving_schema,
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
