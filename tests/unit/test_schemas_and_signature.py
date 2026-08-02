from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from personal_agent.domain.schemas import CommitmentExtraction
from personal_agent.integrations.openwa.signature import OpenWASignatureVerifier


def test_extracted_due_time_is_normalized_to_utc() -> None:
    extraction = CommitmentExtraction(
        kind="commitment",
        summary="Call Daniel back",
        action_type="call",
        due_at=datetime(2026, 7, 31, 14, 15, tzinfo=timezone(timedelta(hours=3))),
        confidence=0.97,
        evidence="time was explicit",
    )

    assert extraction.due_at == datetime(2026, 7, 31, 11, 15, tzinfo=UTC)


def test_extracted_due_time_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        CommitmentExtraction(
            kind="commitment",
            summary="Call Daniel back",
            action_type="call",
            due_at=datetime(2026, 7, 31, 14, 15),
            confidence=0.97,
            evidence="time was explicit",
        )


def test_extracted_explicit_date_accepts_date_only_value() -> None:
    extraction = CommitmentExtraction(
        kind="commitment",
        summary="Call Daniel",
        action_type="call",
        explicit_date="2026-08-01",
        confidence=0.8,
        evidence="tomorrow",
        requires_user_confirmation=True,
    )

    assert extraction.explicit_date is not None
    assert extraction.explicit_date.isoformat() == "2026-08-01"


def test_signature_verifier_accepts_only_matching_body() -> None:
    verifier = OpenWASignatureVerifier("secret")
    body = b'{"event":"message.sent"}'

    assert verifier.verify(body, f"sha256={verifier.sign(body)}") is True
    assert verifier.verify(body + b" ", verifier.sign(body)) is False
    assert verifier.verify(body, None) is False


def test_extraction_normalizes_known_gemini_enum_variants() -> None:
    extraction = CommitmentExtraction(
        kind="personal_commitment",
        summary="Call Daniel",
        action_type="phone_call",
        direction="USER_PROMISED",
        confidence=1,
        evidence="explicit promise",
    )

    assert extraction.kind == "commitment"
    assert extraction.action_type.value == "call"
    assert extraction.direction.value == "user_promised"
