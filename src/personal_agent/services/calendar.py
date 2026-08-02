import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.core.time import require_aware
from personal_agent.domain.enums import ActionClass, ApprovalStatus
from personal_agent.domain.models import ApprovalRequest, AuditLog
from personal_agent.integrations.google_calendar.base import CalendarEvent, CalendarProvider
from personal_agent.integrations.telegram.base import TelegramNotifier

CALENDAR_CREATE_ACTION = "create_calendar_event"


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
