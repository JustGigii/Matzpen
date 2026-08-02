from datetime import datetime

from personal_agent.integrations.telegram.base import Notification


class FakeTelegramNotifier:
    """Captures notifications in memory and never talks to Telegram."""

    def __init__(self) -> None:
        self.notifications: list[Notification] = []

    async def send_text(self, text: str, approval_id: str | None = None) -> str:
        message_id = f"fake-{len(self.notifications) + 1}"
        self.notifications.append(
            Notification(
                kind="text",
                text=text,
                reference_id=approval_id or message_id,
            )
        )
        return message_id

    async def pending_internal_action(
        self, approval_id: str, summary: str, execute_after: datetime
    ) -> str:
        message_id = f"fake-{len(self.notifications) + 1}"
        self.notifications.append(
            Notification(
                kind="pending_internal_action",
                text=f"Reminder proposed: {summary}. Cancel before {execute_after.isoformat()}.",
                reference_id=approval_id,
                scheduled_for=execute_after,
            )
        )
        return message_id

    async def reminder(
        self,
        commitment_id: str,
        summary: str,
        due_at: datetime,
        reminder_id: str | None = None,
    ) -> str:
        message_id = f"fake-{len(self.notifications) + 1}"
        self.notifications.append(
            Notification(
                kind="reminder",
                text=f"Reminder: {summary} (due {due_at.isoformat()}).",
                reference_id=reminder_id or commitment_id,
                scheduled_for=due_at,
            )
        )
        return message_id

    async def approval_request(self, approval_id: str, summary: str) -> str:
        message_id = f"fake-{len(self.notifications) + 1}"
        self.notifications.append(
            Notification(
                kind="approval_request",
                text=f"Approval required: {summary}",
                reference_id=approval_id,
            )
        )
        return message_id

    async def clarification_request(
        self, approval_id: str, summary: str, suggested_hours: tuple[int, ...]
    ) -> str:
        message_id = f"fake-{len(self.notifications) + 1}"
        self.notifications.append(
            Notification(
                kind="clarification_request",
                text=f"Clarification required: {summary}; choices={suggested_hours}",
                reference_id=approval_id,
            )
        )
        return message_id

    async def workflow_confirmation(self, message_id: str | None, text: str) -> str:
        if message_id is not None and message_id.startswith("fake-"):
            index = int(message_id.removeprefix("fake-")) - 1
            if 0 <= index < len(self.notifications):
                existing = self.notifications[index]
                self.notifications[index] = Notification(
                    kind=existing.kind,
                    text=text,
                    reference_id=existing.reference_id,
                )
                return message_id
        return await self.send_text(text)

    async def timetable_request(self, approval_id: str, summary: str, row_count: int) -> str:
        message_id = f"fake-{len(self.notifications) + 1}"
        self.notifications.append(
            Notification(
                kind="timetable_request",
                text=f"{summary}; rows={row_count}",
                reference_id=approval_id,
            )
        )
        return message_id
