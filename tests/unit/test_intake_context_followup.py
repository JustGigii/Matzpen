from personal_agent.domain.enums import EventSource
from personal_agent.domain.models import Event
from personal_agent.services.intake import IntakeService


def test_telegram_context_question_is_not_treated_as_a_new_task() -> None:
    followup = Event(
        source=EventSource.TELEGRAM,
        content_text="מאיזה קבוצה? תן לי יותר פרטים",
    )
    real_task = Event(
        source=EventSource.TELEGRAM,
        content_text="תזכיר לי מחר לברר לגבי שיעור ההכנה",
    )

    assert IntakeService._is_context_followup(followup) is True
    assert IntakeService._is_context_followup(real_task) is False


def test_navigation_and_resolution_messages_are_not_new_items_from_any_source() -> None:
    for source, text in (
        (EventSource.TELEGRAM, "מה משימות שלי"),
        (EventSource.TELEGRAM, "משימה 1 בוצע"),
        (EventSource.WHATSAPP, "מה המשימות שלי?"),
        (EventSource.WHATSAPP, "סיימתי משימה 2"),
    ):
        event = Event(source=source, content_text=text)
        assert IntakeService._is_context_followup(event) is True


def test_misspelled_rewrite_followup_is_routed_to_chat() -> None:
    event = Event(
        source=EventSource.TELEGRAM,
        content_text="תנכל לכנתב את זה יותר יפה",
    )

    assert IntakeService._is_context_followup(event) is True
