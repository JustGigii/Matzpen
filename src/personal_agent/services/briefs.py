import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.core.time import require_aware
from personal_agent.domain.enums import ApprovalStatus, CommitmentStatus, TaskStatus
from personal_agent.domain.models import ApprovalRequest, AuditLog, Commitment, MorningBrief, Task
from personal_agent.domain.schemas import MorningBriefResponse
from personal_agent.integrations.google_calendar.base import CalendarEvent, CalendarProvider
from personal_agent.integrations.telegram.base import TelegramNotifier


class MorningBriefService:
    """Builds and persists the once-per-local-day morning summary."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: TelegramNotifier,
        calendar_provider: CalendarProvider | None,
        now: Callable[[], datetime],
        timezone: str,
        fallback_time: time,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier
        self._calendar_provider = calendar_provider
        self._now = now
        self._timezone = ZoneInfo(timezone)
        self._fallback_time = fallback_time
        self._lock = asyncio.Lock()

    async def trigger(
        self,
        trigger_source: str,
        *,
        force: bool = False,
        send: bool = True,
        at: datetime | None = None,
    ) -> MorningBriefResponse:
        effective_at = require_aware(at or self._now())
        local_date = effective_at.astimezone(self._timezone).date()
        async with self._lock:
            async with self._session_factory() as session:
                existing = await session.scalar(
                    select(MorningBrief).where(MorningBrief.local_date == local_date)
                )
                if existing is not None and not force:
                    return MorningBriefResponse(
                        local_date=local_date.isoformat(),
                        content=existing.content,
                        generated=False,
                        sent=False,
                        trigger_source=existing.trigger_source,
                    )

            content, surfaced_ids = await self._render(effective_at)
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            async with self._session_factory() as session:
                brief = await session.scalar(
                    select(MorningBrief).where(MorningBrief.local_date == local_date)
                )
                if brief is None:
                    brief = MorningBrief(
                        local_date=local_date,
                        trigger_source=trigger_source,
                        triggered_at=effective_at,
                        generated_at=effective_at,
                        sent_at=effective_at if send else None,
                        content=content,
                        content_hash=content_hash,
                        force_requested=force,
                    )
                    session.add(brief)
                else:
                    brief.trigger_source = trigger_source
                    brief.triggered_at = effective_at
                    brief.generated_at = effective_at
                    brief.sent_at = effective_at if send else brief.sent_at
                    brief.content = content
                    brief.content_hash = content_hash
                    brief.force_requested = force
                for commitment_id in surfaced_ids:
                    commitment = await session.get(Commitment, commitment_id)
                    if commitment is not None:
                        commitment.last_daily_nag_at = effective_at
                session.add(
                    AuditLog(
                        actor="morning_brief_service",
                        action="generate_morning_brief",
                        target=local_date.isoformat(),
                        result="delivery_claimed" if send else "generated",
                        redacted_metadata={
                            "trigger_source": trigger_source,
                            "force": force,
                        },
                    )
                )
                await session.commit()
            sent = False
            if send:
                await self._notifier.send_text(content)
                sent = True
            return MorningBriefResponse(
                local_date=local_date.isoformat(),
                content=content,
                generated=True,
                sent=sent,
                trigger_source=trigger_source,
            )

    async def trigger_fallback_if_due(self, at: datetime | None = None) -> bool:
        effective_at = require_aware(at or self._now())
        local_now = effective_at.astimezone(self._timezone)
        if local_now.time().replace(tzinfo=None) < self._fallback_time:
            return False
        result = await self.trigger("scheduled_fallback", at=effective_at)
        return result.generated and result.sent

    async def _render(self, effective_at: datetime) -> tuple[str, list[object]]:
        local_now = effective_at.astimezone(self._timezone)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end = local_start + timedelta(days=1)
        utc_start = local_start.astimezone(UTC)
        utc_end = local_end.astimezone(UTC)
        calendar_events: list[CalendarEvent] = []
        calendar_unavailable = False
        if self._calendar_provider is not None:
            try:
                calendar_events = await self._calendar_provider.list_events(local_start, local_end)
            except Exception:
                calendar_unavailable = True

        async with self._session_factory() as session:
            commitments = list(
                (
                    await session.scalars(
                        select(Commitment).where(
                            Commitment.status.not_in(
                                [CommitmentStatus.DONE, CommitmentStatus.CANCELLED]
                            ),
                            (
                                Commitment.due_at.is_(None)
                                | ((Commitment.due_at >= utc_start) & (Commitment.due_at < utc_end))
                                | (Commitment.status == CommitmentStatus.OVERDUE)
                            ),
                        )
                    )
                ).all()
            )
            tasks = list(
                (
                    await session.scalars(
                        select(Task).where(
                            Task.status == TaskStatus.PENDING,
                            Task.due_at.is_(None)
                            | ((Task.due_at >= utc_start) & (Task.due_at < utc_end)),
                        )
                    )
                ).all()
            )
            approvals = list(
                (
                    await session.scalars(
                        select(ApprovalRequest).where(
                            ApprovalRequest.status == ApprovalStatus.PENDING
                        )
                    )
                ).all()
            )

        entries: list[tuple[datetime | None, str]] = []
        for event in calendar_events:
            entries.append((event.start, f"🗓️ {self._local_time(event.start)} — {event.summary}"))
        surfaced_ids: list[object] = []
        for commitment in commitments:
            surfaced_ids.append(commitment.id)
            if commitment.status is CommitmentStatus.OVERDUE:
                text = f"⚠️ באיחור — {commitment.summary}"
            elif commitment.due_at is None:
                text = f"📌 עדיין פתוח — {commitment.summary}"
            else:
                text = f"⏰ עד {self._local_time(commitment.due_at)} — {commitment.summary}"
            entries.append((commitment.due_at, text))
        for task in tasks:
            text = (
                f"📌 עדיין פתוחה — {task.title}"
                if task.due_at is None
                else f"☑️ משימה עד {self._local_time(task.due_at)} — {task.title}"
            )
            entries.append((task.due_at, text))
        entries.sort(key=lambda entry: entry[0] or utc_end)

        lines = ["🌤️ בוקר טוב", "━━━━━━━━━━━━", "📅 היום:"]
        lines.extend(f"{index}. {text}" for index, (_, text) in enumerate(entries, start=1))
        if not entries:
            lines.append("✨ אין היום פריטים מתוזמנים או התחייבויות פתוחות.")
        if approvals:
            lines.append(f"\n✋ ממתינות {len(approvals)} בקשות אישור או הבהרה.")
        tight = self._tight_transitions(calendar_events)
        if tight:
            lines.append(f"\n⚠️ מעבר צפוף: {tight}")
        if entries:
            lines.append("\n🎯 עדיפות מוצעת: להתחיל בפריט 1.")
        if calendar_unavailable:
            lines.append("\n📵 Google Calendar אינו זמין כרגע; הסיכום הפנימי נשמר.")
        return "\n".join(lines), surfaced_ids

    def _local_time(self, value: datetime | None) -> str:
        if value is None:
            return "ללא שעה"
        return value.astimezone(self._timezone).strftime("%H:%M")

    def _tight_transitions(self, events: list[CalendarEvent]) -> str | None:
        ordered = sorted(events, key=lambda event: event.start)
        for previous, current in pairwise(ordered):
            gap = current.start - previous.end
            if timedelta(0) <= gap < timedelta(minutes=15):
                minutes = int(gap.total_seconds() / 60)
                return f"{previous.summary} ואז {current.summary} בתוך {minutes} דקות"
            if gap < timedelta(0):
                return f"התנגשות בין {previous.summary} לבין {current.summary}"
        return None
