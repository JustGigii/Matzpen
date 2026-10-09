from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Notification:
    kind: str
    text: str
    reference_id: str
    scheduled_for: datetime | None = None


class TextNotifier(Protocol):
    async def send_text(self, text: str, approval_id: str | None = None) -> str: ...


class TelegramNotifier(TextNotifier, Protocol):
    async def pending_internal_action(
        self, approval_id: str, summary: str, execute_after: datetime
    ) -> str: ...

    async def reminder(
        self,
        commitment_id: str,
        summary: str,
        due_at: datetime,
        reminder_id: str | None = None,
    ) -> str: ...

    async def approval_request(self, approval_id: str, summary: str) -> str: ...

    async def clarification_request(
        self, approval_id: str, summary: str, suggested_hours: tuple[int, ...]
    ) -> str: ...

    async def detail_clarification_request(
        self,
        approval_id: str,
        summary: str,
        question: str,
        options: tuple[str, ...],
    ) -> str: ...

    async def workflow_confirmation(self, message_id: str | None, text: str) -> str: ...

    async def timetable_request(self, approval_id: str, summary: str, row_count: int) -> str: ...

    async def group_tracking_request(self, conversation_id: str, display_name: str) -> str: ...

    async def memory_request(self, approval_id: str, summary: str) -> str: ...
