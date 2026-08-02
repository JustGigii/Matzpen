from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from personal_agent.domain.enums import EventDirection
from personal_agent.domain.schemas import ExtractionRequest, ExtractionResult
from personal_agent.integrations.llm.base import LLMProvider
from personal_agent.integrations.telegram.base import TextNotifier
from personal_agent.integrations.telegram.presentation import (
    DIVIDER,
    local_datetime,
    localized_summary,
)
from personal_agent.services.confirmations import ConfirmationService


def render_extraction(result: ExtractionResult, timezone: str) -> str:
    if not result.items:
        return "לא זוהו התחייבויות או פעולות בטקסט שנשלח."

    local_timezone = ZoneInfo(timezone)
    lines = ["🧠 זיהוי הסתיים", DIVIDER, f"זוהו {len(result.items)} פריטים:"]
    for index, item in enumerate(result.items, start=1):
        due = (
            local_datetime(item.due_at, local_timezone)
            if item.due_at is not None
            else "ללא שעה שנקבעה"
        )
        person_name = item.person.display_name if item.person is not None else None
        summary = localized_summary(item.summary, item.action_type.value, person_name)
        lines.extend(
            [
                "",
                f"{index}. 📝 {summary}",
                f"🕓 מועד: {due}",
            ]
        )
        if item.requires_user_confirmation:
            lines.append("✋ נדרש אישור")
    return "\n".join(lines)


class DirectMessageService:
    def __init__(
        self,
        llm_provider: LLMProvider,
        notifier: TextNotifier,
        timezone: str,
        now: Callable[[], datetime],
        confirmation_service: ConfirmationService | None = None,
    ) -> None:
        self._llm_provider = llm_provider
        self._notifier = notifier
        self._timezone = timezone
        self._now = now
        self._confirmation_service = confirmation_service

    async def process_and_notify(self, message: str) -> tuple[ExtractionResult, str]:
        content = message.strip()
        if not content:
            raise ValueError("Message cannot be empty")
        occurred_at = self._now()
        result = await self._llm_provider.extract_event(
            ExtractionRequest(
                event_id=f"direct-{occurred_at.timestamp()}",
                event_type="direct.message",
                direction=EventDirection.INBOUND,
                occurred_at=occurred_at,
                content_text=content,
            )
        )
        approval_id = None
        if any(item.requires_user_confirmation for item in result.items):
            if self._confirmation_service is None:
                raise RuntimeError("A confirmation service is required for ambiguous extractions")
            approval_id = await self._confirmation_service.create(result)
        telegram_message_id = await self._notifier.send_text(
            render_extraction(result, self._timezone),
            str(approval_id) if approval_id is not None else None,
        )
        return result, telegram_message_id
