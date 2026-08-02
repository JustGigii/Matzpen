from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from personal_agent.integrations.telegram.presentation import (
    approval_card,
    clarification_card,
    friendly_local_datetime,
    localized_summary,
    pending_action_card,
    reminder_card,
    timetable_card,
)


def test_action_cards_are_scannable_and_use_local_time() -> None:
    timezone = ZoneInfo("Asia/Jerusalem")
    scheduled_for = datetime(2026, 8, 1, 14, 15, tzinfo=UTC)

    pending = pending_action_card("פגישה עם יובל", scheduled_for, timezone)
    reminder = reminder_card("פגישה עם יובל", scheduled_for, timezone)

    assert "🧠 זיהיתי התחייבות" in pending
    assert "01.08 בשעה 17:15" in pending
    assert "📝 פגישה עם יובל" in pending
    assert "⏰ תזכורת" in reminder
    assert "📅 מועד: 01.08 בשעה 17:15" in reminder


def test_decision_cards_explain_the_next_action() -> None:
    assert "אישור מפורש" in approval_card("📝 אירוע ביומן")
    assert "לבחור" in clarification_card("📝 פגישה בערב")
    assert "כל שורה" in timetable_card("• מתמטיקה · יום א")


def test_english_action_summary_uses_a_hebrew_fallback() -> None:
    assert localized_summary("call Navel", "call", "Navel") == "שיחה עם Navel"
    assert localized_summary("Call Daniel", "call") == "שיחה עם Daniel"
    assert localized_summary("פגישה עם יובל", "meet", "יובל") == "פגישה עם יובל"


def test_friendly_time_avoids_iso_formatting() -> None:
    timezone = ZoneInfo("Asia/Jerusalem")
    reference = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)

    assert (
        friendly_local_datetime(reference + timedelta(hours=2), reference, timezone)
        == "היום ב־15:00"
    )
    assert (
        friendly_local_datetime(reference + timedelta(days=1), reference, timezone) == "מחר ב־13:00"
    )
