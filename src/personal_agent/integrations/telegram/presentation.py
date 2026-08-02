from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DIVIDER = "━━━━━━━━━━━━"

HEBREW_ACTION_SUMMARIES = {
    "call": "שיחה עם",
    "message": "הודעה ל־",
    "send": "שליחה ל־",
    "meet": "פגישה עם",
    "pay": "תשלום ל־",
}


def local_datetime(value: datetime, timezone: ZoneInfo) -> str:
    return value.astimezone(timezone).strftime("%d.%m בשעה %H:%M")


def friendly_local_datetime(value: datetime, reference_at: datetime, timezone: ZoneInfo) -> str:
    local_value = value.astimezone(timezone)
    reference_date = reference_at.astimezone(timezone).date()
    if local_value.date() == reference_date:
        return f"היום ב־{local_value:%H:%M}"
    if local_value.date() == reference_date + timedelta(days=1):
        return f"מחר ב־{local_value:%H:%M}"
    return f"ב־{local_value:%d.%m} בשעה {local_value:%H:%M}"


def card(icon: str, title: str, *lines: str) -> str:
    body = "\n".join(line for line in lines if line)
    return f"{icon} {title}\n{DIVIDER}\n{body}"


def localized_summary(summary: str, action_type: str, person_name: str | None = None) -> str:
    """Prefer a concise Hebrew action label when Gemini returned an English summary."""
    cleaned = summary.strip().removeprefix("📝").strip()
    if any("\u0590" <= character <= "\u05ff" for character in cleaned):
        return cleaned
    action = HEBREW_ACTION_SUMMARIES.get(action_type)
    if action is None:
        return cleaned
    words = cleaned.split(maxsplit=1)
    if len(words) == 2 and words[0].lower().rstrip(":") == action_type:
        return f"{action} {words[1]}"
    return f"{action} {person_name}" if person_name else action


def pending_action_card(summary: str, execute_after: datetime, timezone: ZoneInfo) -> str:
    return card(
        "🧠",
        "זיהיתי התחייבות",
        f"📝 {summary}",
        "",
        f"⏳ אשמור אותה ב־{local_datetime(execute_after, timezone)}.",
        "אפשר לבצע עכשיו, לשנות או לבטל.",
    )


def reminder_card(summary: str, due_at: datetime, timezone: ZoneInfo) -> str:
    return card(
        "⏰",
        "תזכורת",
        f"📝 {summary}",
        f"📅 מועד: {local_datetime(due_at, timezone)}",
        "",
        "אפשר לסמן בוצע, לדחות או לבחור זמן חדש.",
    )


def approval_card(summary: str) -> str:
    return card(
        "✋",
        "נדרש אישור",
        f"📝 {summary.removeprefix('📝 ').strip()}",
        "",
        "הפעולה תתבצע רק לאחר אישור מפורש שלך.",
    )


def clarification_card(summary: str) -> str:
    return card(
        "🤔",
        "צריך לבחור שעה",
        f"📝 {summary.removeprefix('📝 ').strip()}",
        "",
        "אפשר לבחור אחת מהשעות המוצעות או לקבוע שעה אחרת.",
    )


def timetable_card(summary: str) -> str:
    return card(
        "📚",
        "זוהתה מערכת שעות",
        summary,
        "",
        "אפשר להוסיף את כל השורות או לכלול ולהחריג כל שורה בנפרד.",
    )


def success_card(title: str, *lines: str) -> str:
    return card("✅", title, *lines)
