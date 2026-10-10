import re
from dataclasses import dataclass

from personal_agent.domain.enums import CommitmentDirection, EventDirection, EventSource
from personal_agent.domain.schemas import CommitmentExtraction

_WORD_PATTERN = re.compile(r"[\w\u0590-\u05ff]+", re.UNICODE)
_QUOTE_PREFIX_PATTERN = re.compile(
    r"^\s*(?:quote|evidence|quoted\s+evidence|ציטוט|ראיה|מתוך\s+השיחה)\s*[:\-]\s*",
    re.IGNORECASE,
)
_CONTROL_MESSAGE_PATTERN = re.compile(
    r"^\s*(?:"
    r"(?:תציג|תראה|הצג|הראה|רשום)\b.{0,35}(?:משימות|התחייבויות|רשימה|פתוחות)|"
    r"(?:מחק|תמחק|בטל|תבטל|סמן)\b.{0,45}(?:ה?משימ\w*|התחייב\w*|ה?כל(?:\s+ה?משימ\w*)?|כולם|כולן|מספר\s*\d+|\d+)|"
    r"(?:אפשר|כן|לא)\s+(?:הכל|כולם|כולן)|"
    r"(?:זה|זאת)\s+לא\s+רלוונטי(?:ת)?|"
    r"(?:לא\s+הבנת|הבנת\s+לא\s+נכון|למה\s+(?:יצרת|הוספת))|"
    r"(?:show|list)\b.{0,35}(?:tasks?|commitments?)|"
    r"(?:delete|remove|cancel|complete)\b.{0,40}(?:all|tasks?|commitments?|number\s*\d+|\d+)"
    r")\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_UNRESOLVED_REMINDER_PATTERN = re.compile(
    r"(?:תזכיר|להזכיר)\s+לי\b.{0,80}\b(?:את\s+זה|אותו|אותה|אותם|אותן)\b|"
    r"remind\s+me\b.{0,80}\b(?:it|that)\b",
    re.IGNORECASE,
)
_HEBREW_DIRECT_REQUEST_PATTERN = re.compile(
    r"(?:^|\s)(?:(?:תוכל|תוכלי|בבקשה|נא|אל\s+תשכח|אל\s+תשכחי|צריך\s+שת|צריכה\s+שת)|"
    r"(?:תגיע|תגיעי)|(?:תשלח|תשלחי|תתקשר|תתקשרי|תקבע|תקבעי|תביא|תביאי|תכין|תכיני)"
    r"\b.{0,40}\b(?:לי|אלי|איתי))\b",
    re.IGNORECASE,
)
_ENGLISH_DIRECT_REQUEST_PATTERN = re.compile(
    r"\b(?:can|could|would|will)\s+you\b|\bplease\b|\bdon't\s+forget\s+to\b",
    re.IGNORECASE,
)
_HEBREW_EXPLICIT_PROMISE_PATTERN = re.compile(
    r"(?:^|\s)אני\s+(?:(?:מבטיח|מתחייב)(?:ה)?\b|(?:א|נ)\S+\b.{0,80}"
    r"(?:לך|לכם|לכן|איתך|איתכם|איתכן|אותך|אתכם|אתכן|לנו)\b)",
    re.IGNORECASE,
)
_ENGLISH_EXPLICIT_PROMISE_PATTERN = re.compile(
    r"\bI(?:\s+(?:promise|commit)\s+to\b|(?:\s+will|'ll)\b.{0,80}"
    r"\b(?:you|us|with\s+you)\b)",
    re.IGNORECASE,
)
_CONCRETE_SHARED_APPOINTMENT_PATTERN = re.compile(
    r"(?:^|\s)(?:קבעתי|קבענו|הזמנתי|הזמנו|קבעו)\s+לנו\b|"
    r"(?:^|\s)יש\s+לנו\s+(?:תור|פגישה)\b|"
    r"(?:^|\s)בוא(?:י|ו)?\s+(?:נקבע|ניפגש)\b|"  # noqa: RUF001
    r"\b(?:I|we)(?:'ve|\s+have)\s+(?:booked|scheduled)\b.{0,60}\b(?:us|for\s+us)\b",
    re.IGNORECASE,
)
_ACTIONISH_PATTERN = re.compile(
    r"(?:^|\s)ל[\u0590-\u05ff]{3,}\b|"
    r"\b(?:call|send|message|meet|pay|buy|get|book|schedule|submit|finish|complete|prepare)\b",
    re.IGNORECASE,
)
_LOW_INFORMATION_WORDS = {
    "אני",
    "את",
    "זה",
    "זאת",
    "אותו",
    "אותה",
    "אותם",
    "אותן",
    "לי",
    "לך",
    "כל",
    "הכל",
    "היום",
    "מחר",
    "בערב",
    "בבוקר",
    "it",
    "that",
    "this",
    "me",
    "you",
    "today",
    "tomorrow",
}


@dataclass(frozen=True)
class ExtractionQualityDecision:
    accepted: bool
    reason: str | None = None


def assess_extraction_quality(
    source_text: str,
    item: CommitmentExtraction,
    *,
    source: EventSource | str,
    event_direction: EventDirection | str,
    conversation_type: str | None = None,
) -> ExtractionQualityDecision:
    """Apply source-grounded, deterministic checks before persisting an extraction.

    This is deliberately a guard rather than another classifier. It rejects only cases
    that are unsafe to infer without dialogue context and leaves semantic judgment to
    the extraction model and the existing approval policy.
    """

    source_text = source_text.strip()
    summary = item.summary.strip()
    evidence = _QUOTE_PREFIX_PATTERN.sub("", item.evidence).strip().strip("\"“”'")

    if not source_text or not summary or not evidence:
        return ExtractionQualityDecision(False, "empty_source_summary_or_evidence")
    if _CONTROL_MESSAGE_PATTERN.fullmatch(source_text):
        return ExtractionQualityDecision(False, "assistant_control_or_feedback")
    if _UNRESOLVED_REMINDER_PATTERN.search(source_text):
        return ExtractionQualityDecision(False, "unresolved_context_reference")
    source_value = source.value if isinstance(source, EventSource) else source
    if source_value == EventSource.TELEGRAM.value and not _evidence_is_grounded(
        source_text, evidence
    ):
        return ExtractionQualityDecision(False, "evidence_not_grounded")
    if _is_low_information_summary(summary, item):
        return ExtractionQualityDecision(False, "low_information_summary")

    direction_value = (
        event_direction.value if isinstance(event_direction, EventDirection) else event_direction
    )
    if (
        source_value == EventSource.WHATSAPP.value
        and direction_value == EventDirection.INBOUND.value
        and conversation_type == "private"
        and not _is_explicit_private_whatsapp_obligation(source_text, item.direction)
    ):
        return ExtractionQualityDecision(False, "implicit_private_chat_statement")

    return ExtractionQualityDecision(True)


def filter_extraction_items(
    source_text: str,
    items: list[CommitmentExtraction],
    *,
    source: EventSource | str,
    event_direction: EventDirection | str,
    conversation_type: str | None = None,
) -> list[CommitmentExtraction]:
    return [
        item
        for item in items
        if assess_extraction_quality(
            source_text,
            item,
            source=source,
            event_direction=event_direction,
            conversation_type=conversation_type,
        ).accepted
    ]


def _evidence_is_grounded(source_text: str, evidence: str) -> bool:
    normalized_source = _normalize_for_grounding(source_text)
    normalized_evidence = _normalize_for_grounding(evidence)
    return bool(normalized_evidence and normalized_evidence in normalized_source)


def _normalize_for_grounding(value: str) -> str:
    return " ".join(_words(value.casefold().replace("…", " ")))


def _words(value: str) -> list[str]:
    return _WORD_PATTERN.findall(value)


def _is_low_information_summary(summary: str, item: CommitmentExtraction) -> bool:
    if item.calendar_worthy or item.timetable_rows:
        return False
    words = _words(summary.casefold())
    substantive = [word for word in words if word not in _LOW_INFORMATION_WORDS]
    if len(substantive) < 2:
        return True
    return item.action_type.value == "other" and not _ACTIONISH_PATTERN.search(summary)


def _is_explicit_private_whatsapp_obligation(
    evidence: str,
    commitment_direction: CommitmentDirection,
) -> bool:
    if _CONCRETE_SHARED_APPOINTMENT_PATTERN.search(evidence):
        return True
    if commitment_direction is CommitmentDirection.USER_PROMISED:
        return bool(
            _HEBREW_DIRECT_REQUEST_PATTERN.search(evidence)
            or _ENGLISH_DIRECT_REQUEST_PATTERN.search(evidence)
        )
    return bool(
        _HEBREW_EXPLICIT_PROMISE_PATTERN.search(evidence)
        or _ENGLISH_EXPLICIT_PROMISE_PATTERN.search(evidence)
    )
