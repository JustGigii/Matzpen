import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.core.time import require_aware
from personal_agent.domain.enums import (
    ActionClass,
    ApprovalStatus,
    CalendarActionStatus,
    CommitmentStatus,
    TaskStatus,
)
from personal_agent.domain.models import (
    ApprovalRequest,
    AuditLog,
    CalendarAction,
    Commitment,
    Task,
)
from personal_agent.integrations.google_calendar.base import CalendarEvent, CalendarProvider
from personal_agent.integrations.telegram.base import TelegramNotifier

CALENDAR_CREATE_ACTION = "create_calendar_event"
CalendarItemResult = Literal["created", "already_exists", "missing", "closed", "untimed"]
CalendarApprovalSource = Literal["explicit_telegram_button", "explicit_telegram_text"]


class CalendarService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: CalendarProvider,
        notifier: TelegramNotifier,
        now: Callable[[], datetime],
        timezone: str = "Asia/Jerusalem",
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._notifier = notifier
        self._now = now
        self._timezone = ZoneInfo(timezone)

    async def list_upcoming(self, days: int = 7) -> list[CalendarEvent]:
        start = require_aware(self._now())
        return await self._provider.list_events(start, start + timedelta(days=days))

    async def list_today(self) -> list[CalendarEvent]:
        local_now = require_aware(self._now()).astimezone(self._timezone)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end = local_start + timedelta(days=1)
        return await self._provider.list_events(local_start, local_end)

    async def propose_event(
        self,
        summary: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
    ) -> uuid.UUID:
        start = require_aware(start)
        end = require_aware(end)
        if end <= start:
            raise ValueError("Calendar event end must be after its start")
        approval_id = uuid.uuid4()
        event_id = f"pa{approval_id.hex}"
        approval = ApprovalRequest(
            id=approval_id,
            action_type=CALENDAR_CREATE_ACTION,
            action_payload={
                "external_id": event_id,
                "summary": summary,
                "description": description,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            risk_class=ActionClass.DESTRUCTIVE_OR_SENSITIVE.value,
            status=ApprovalStatus.PENDING,
        )
        async with self._session_factory() as session:
            session.add(approval)
            session.add(
                AuditLog(
                    actor="calendar_service",
                    action="propose_calendar_event",
                    target=event_id,
                    policy_decision="explicit_approval_required",
                    approval_id=approval_id,
                    result="pending",
                )
            )
            await session.commit()
            message_id = await self._notifier.approval_request(
                str(approval_id),
                f"יצירת אירוע: {summary}\n{start.isoformat()} — {end.isoformat()}",
            )
            approval.telegram_message_id = message_id
            await session.commit()
        return approval_id

    async def resolve_proposal(self, approval_id: uuid.UUID, *, approve: bool) -> bool:
        async with self._session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if (
                approval is None
                or approval.action_type != CALENDAR_CREATE_ACTION
                or approval.status is not ApprovalStatus.PENDING
            ):
                return False
            if not approve:
                approval.status = ApprovalStatus.REJECTED
                approval.resolved_at = require_aware(self._now())
                await session.commit()
                return True
            approval.status = ApprovalStatus.APPROVED
            await session.commit()
            payload = approval.action_payload
            try:
                created = await self._provider.create_event(
                    CalendarEvent(
                        external_id=str(payload["external_id"]),
                        summary=str(payload["summary"]),
                        description=payload.get("description"),
                        start=datetime.fromisoformat(str(payload["start"])),
                        end=datetime.fromisoformat(str(payload["end"])),
                    )
                )
            except Exception:
                approval.status = ApprovalStatus.FAILED
                approval.resolved_at = require_aware(self._now())
                await session.commit()
                raise
            approval.action_payload = {**payload, "google_event_id": created.external_id}
            approval.status = ApprovalStatus.EXECUTED
            approval.resolved_at = require_aware(self._now())
            session.add(
                AuditLog(
                    actor="calendar_service",
                    action="create_calendar_event",
                    target=created.external_id,
                    policy_decision="explicitly_approved",
                    approval_id=approval.id,
                    result="executed",
                )
            )
            await session.commit()
            return True

    async def add_items_to_calendar(
        self,
        item_ids: Iterable[uuid.UUID],
        *,
        approval_source: CalendarApprovalSource = "explicit_telegram_text",
    ) -> dict[uuid.UUID, CalendarItemResult]:
        """Add only explicitly selected items, preserving individual scheduling results."""
        results: dict[uuid.UUID, CalendarItemResult] = {}
        for item_id in dict.fromkeys(item_ids):
            results[item_id] = await self.add_item_to_calendar(
                item_id, approval_source=approval_source
            )
        return results

    async def add_item_to_calendar(
        self,
        item_id: uuid.UUID,
        *,
        approval_source: CalendarApprovalSource = "explicit_telegram_button",
    ) -> CalendarItemResult:
        """Create a short Calendar block after an explicit Telegram request.

        The request itself is the user's approval. A stable action key and event ID make repeated
        taps idempotent, while the linked CalendarAction keeps the task/commitment status visible.
        """

        if approval_source not in {"explicit_telegram_button", "explicit_telegram_text"}:
            raise ValueError("Calendar writes require an explicit Telegram request")
        executed_at = require_aware(self._now())
        async with self._session_factory() as session:
            commitment = await session.get(Commitment, item_id)
            task = await session.get(Task, item_id) if commitment is None else None
            if commitment is None and task is None:
                return "missing"
            source_event_id: uuid.UUID | None
            if commitment is not None:
                if commitment.status in {CommitmentStatus.DONE, CommitmentStatus.CANCELLED}:
                    return "closed"
                summary = commitment.summary
                due_at = commitment.due_at
                source_event_id = commitment.source_event_id
                item_kind = "commitment"
            else:
                assert task is not None
                if task.status in {TaskStatus.DONE, TaskStatus.CANCELLED}:
                    return "closed"
                summary = task.title
                due_at = task.due_at
                source_event_id = task.source_event_id
                item_kind = "task"
            if due_at is None:
                return "untimed"
            if source_event_id is None:
                return "missing"

            dedupe_key = f"manual-calendar:{item_kind}:{item_id}"
            action = await session.scalar(
                select(CalendarAction).where(CalendarAction.dedupe_key == dedupe_key)
            )
            if action is not None and action.status is CalendarActionStatus.EXECUTED:
                return "already_exists"
            payload = {
                "summary": summary,
                "description": (
                    "נוסף מהסוכן האישי בבקשה מפורשת. הפריט נשאר פתוח עד לסימון 'סיימתי'."
                ),
                "start": due_at.isoformat(),
                "end": (due_at + timedelta(minutes=30)).isoformat(),
                "recurrence": [],
                "attendees": [],
                "item_kind": item_kind,
                "item_id": str(item_id),
            }
            if action is None:
                action = CalendarAction(
                    source_event_id=source_event_id,
                    commitment_id=commitment.id if commitment is not None else None,
                    operation="create",
                    payload_json=payload,
                    status=CalendarActionStatus.PENDING,
                    dedupe_key=dedupe_key,
                )
                session.add(action)
                await session.flush()
            else:
                action.payload_json = payload
                action.status = CalendarActionStatus.PENDING

            try:
                created = await self._provider.create_event(
                    CalendarEvent(
                        external_id=f"pa{action.id.hex}",
                        summary=summary,
                        description=str(payload["description"]),
                        start=due_at,
                        end=due_at + timedelta(minutes=30),
                    )
                )
            except Exception as exc:
                action.status = CalendarActionStatus.FAILED
                session.add(
                    AuditLog(
                        actor="calendar_service",
                        action="add_item_to_calendar",
                        target=str(item_id),
                        policy_decision=approval_source,
                        result="failed",
                        redacted_metadata={"error_type": type(exc).__name__},
                    )
                )
                await session.commit()
                raise

            action.google_event_id = created.external_id
            action.status = CalendarActionStatus.EXECUTED
            action.executed_at = executed_at
            if commitment is not None:
                commitment.calendar_action_id = action.id
            session.add(
                AuditLog(
                    actor="calendar_service",
                    action="add_item_to_calendar",
                    target=str(item_id),
                    policy_decision=approval_source,
                    result="executed",
                )
            )
            await session.commit()
            return "created"
