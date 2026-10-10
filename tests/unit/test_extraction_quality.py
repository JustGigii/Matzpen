from personal_agent.domain.schemas import CommitmentExtraction
from personal_agent.services.extraction_quality import (
    assess_extraction_quality,
    filter_extraction_items,
)


def _item(
    summary: str,
    evidence: str,
    *,
    direction: str = "user_promised",
    action_type: str = "other",
) -> CommitmentExtraction:
    return CommitmentExtraction(
        kind="task",
        summary=summary,
        action_type=action_type,
        direction=direction,
        confidence=1.0,
        evidence=evidence,
    )


def test_rejects_assistant_control_as_a_new_task() -> None:
    item = _item("למחוק את כל המשימות", "תמחק את כל המשימות")

    decision = assess_extraction_quality(
        "תמחק את כל המשימות",
        item,
        source="telegram",
        event_direction="inbound",
    )

    assert decision.accepted is False
    assert decision.reason == "assistant_control_or_feedback"


def test_rejects_reminder_with_an_unresolved_pronoun() -> None:
    item = _item(
        "להזכיר לי כל יום עד שאעשה את זה",
        "להזכיר לי כל יום עד שאני יעשה את זה",
    )

    decision = assess_extraction_quality(
        "להזכיר לי כל יום עד שאני יעשה את זה",
        item,
        source="telegram",
        event_direction="inbound",
    )

    assert decision.accepted is False
    assert decision.reason == "unresolved_context_reference"


def test_rejects_evidence_that_does_not_appear_in_the_source() -> None:
    item = _item("להתקשר לדני", "אני אתקשר לדני מחר", action_type="call")

    decision = assess_extraction_quality(
        "דיברנו על מזג האוויר",
        item,
        source="telegram",
        event_direction="inbound",
    )

    assert decision.accepted is False
    assert decision.reason == "evidence_not_grounded"


def test_rejects_low_information_chat_fragment() -> None:
    item = _item("פנוי בערב", "פנוי בערב")

    decision = assess_extraction_quality(
        "פנוי בערב",
        item,
        source="telegram",
        event_direction="inbound",
    )

    assert decision.accepted is False
    assert decision.reason == "low_information_summary"


def test_preserves_explicit_telegram_task() -> None:
    item = _item("להתקשר לדני", "להתקשר לדני מחר", action_type="call")

    decision = assess_extraction_quality(
        "להתקשר לדני מחר",
        item,
        source="telegram",
        event_direction="inbound",
    )

    assert decision.accepted is True


def test_preserves_explicit_private_whatsapp_request() -> None:
    source_text = "תוכל לשלוח לי את הצעת המחיר מחר?"
    item = _item(
        "לשלוח הצעת מחיר",
        source_text,
        direction="user_promised",
        action_type="send",
    )

    decision = assess_extraction_quality(
        source_text,
        item,
        source="whatsapp",
        event_direction="inbound",
        conversation_type="private",
    )

    assert decision.accepted is True


def test_preserves_direct_private_whatsapp_arrival_request() -> None:
    source_text = "תגיע מחר בשעה שתיים"
    item = _item(
        "להגיע מחר בשעה שתיים",
        source_text,
        direction="user_promised",
        action_type="meet",
    )

    decision = assess_extraction_quality(
        source_text,
        item,
        source="whatsapp",
        event_direction="inbound",
        conversation_type="private",
    )

    assert decision.accepted is True


def test_preserves_booked_private_whatsapp_appointment_for_both_parties() -> None:
    source_text = "קבעתי לנו פגישה למחר בשעה שתיים"
    item = _item(
        "פגישה מחר בשעה שתיים",
        source_text,
        direction="other_promised",
        action_type="meet",
    )

    decision = assess_extraction_quality(
        source_text,
        item,
        source="whatsapp",
        event_direction="inbound",
        conversation_type="private",
    )

    assert decision.accepted is True


def test_preserves_explicit_private_whatsapp_promise_to_user() -> None:
    source_text = "אני אשלח לך את הקישור מחר"
    item = _item(
        "לשלוח את הקישור",
        source_text,
        direction="other_promised",
        action_type="send",
    )

    decision = assess_extraction_quality(
        source_text,
        item,
        source="whatsapp",
        event_direction="inbound",
        conversation_type="private",
    )

    assert decision.accepted is True


def test_rejects_ordinary_private_whatsapp_plan_about_someone_else() -> None:
    source_text = "אני מזמין אותה מחר ללכת למים חמים"
    item = _item(
        "להזמין אותה ללכת למים חמים",
        source_text,
        direction="other_promised",
    )

    decision = assess_extraction_quality(
        source_text,
        item,
        source="whatsapp",
        event_direction="inbound",
        conversation_type="private",
    )

    assert decision.accepted is False
    assert decision.reason == "implicit_private_chat_statement"


def test_filter_keeps_only_grounded_actionable_items() -> None:
    source_text = "להתקשר לדני מחר"
    valid = _item("להתקשר לדני", source_text, action_type="call")
    fabricated = _item("לשלוח הצעה לשרה", "לשלוח הצעה לשרה", action_type="send")

    assert filter_extraction_items(
        source_text,
        [valid, fabricated],
        source="telegram",
        event_direction="inbound",
    ) == [valid]


def test_preserves_english_other_action_task() -> None:
    source_text = "Get a moving quote from Dana"
    item = _item(source_text, source_text)

    decision = assess_extraction_quality(
        source_text,
        item,
        source="telegram",
        event_direction="inbound",
    )

    assert decision.accepted is True
