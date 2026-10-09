from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from personal_agent.domain.enums import CalendarActionStatus, CommitmentStatus, EventSource
from personal_agent.domain.models import CalendarAction, Commitment, Event
from personal_agent.integrations.telegram.presentation import (
    approval_card,
    clarification_card,
    friendly_local_datetime,
    localized_summary,
    pending_action_card,
    reminder_card,
    timetable_card,
)
from personal_agent.integrations.telegram.runtime import TelegramRuntime


def test_action_cards_are_scannable_and_use_local_time() -> None:
    timezone = ZoneInfo("Asia/Jerusalem")
    scheduled_for = datetime(2026, 8, 1, 14, 15, tzinfo=UTC)

    pending = pending_action_card("פגישה עם יובל", scheduled_for, timezone)
    reminder = reminder_card("פגישה עם יובל", scheduled_for, timezone)

    assert "🧠 זיהיתי התחייבות" in pending
    assert "בעוד כדקה" in pending
    assert "📝 פגישה עם יובל" in pending
    assert "🔔 צריך החלטה" in reminder
    assert "📅 מועד: 01.08 בשעה 17:15" in reminder
    assert "סיימתי = בוצע" in reminder
    assert "לא רלוונטי = מסיר" in reminder


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


def test_commitment_detail_includes_due_source_status_and_calendar() -> None:
    runtime = object.__new__(TelegramRuntime)
    runtime._timezone = ZoneInfo("Asia/Jerusalem")
    commitment = Commitment(
        summary="שיחת Zoom עם Shaked Aviv",
        due_at=datetime(2026, 8, 4, 16, 0, tzinfo=UTC),
        status=CommitmentStatus.SCHEDULED,
    )
    source = Event(
        source=EventSource.WHATSAPP,
        payload_json={"conversation_display_name": "Shaked Aviv"},
    )
    calendar_action = CalendarAction(status=CalendarActionStatus.EXECUTED)

    detail = runtime._commitment_detail(commitment, source, calendar_action)

    assert "04.08.2026 בשעה 19:00" in detail
    assert "סטטוס: מתוזמנת" in detail  # noqa: RUF001
    assert "WhatsApp עם Shaked Aviv" in detail
    assert "נוסף ל־Google Calendar" in detail
