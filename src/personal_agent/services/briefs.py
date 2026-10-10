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
        self._check_in_lock = asyncio.Lock()

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
                        sent_at=None,
                        content=content,
                        content_hash=content_hash,
                        force_requested=force,
                    )
                    session.add(brief)
                else:
                    brief.trigger_source = trigger_source
                    brief.triggered_at = effective_at
                    brief.generated_at = effective_at
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
                async with self._session_factory() as session:
                    delivered_brief = await session.scalar(
                        select(MorningBrief).where(MorningBrief.local_date == local_date)
                    )
                    if delivered_brief is not None:
                        delivered_brief.sent_at = effective_at
                    await session.commit()
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
        local_date = local_now.date().isoformat()
        async with self._check_in_lock:
            async with self._session_factory() as session:
                delivered = await session.scalar(
                    select(AuditLog.id).where(
                        AuditLog.action == "daily_morning_check_in",
                        AuditLog.target == local_date,
                        AuditLog.result == "sent",
                    )
                )
                if delivered is not None:
                    return False
            # A wake-up summary must not suppress the scheduled daily questions.
            result = await self.trigger("scheduled_fallback", force=True, at=effective_at)
            if result.sent:
                async with self._session_factory() as session:
                    session.add(
                        AuditLog(
                            actor="morning_brief_service",
                            action="daily_morning_check_in",
                            target=local_date,
                            result="sent",
                            timestamp=effective_at,
                        )
                    )
                    await session.commit()
            return result.generated and result.sent

    async def _render(self, effective_at: datetime) -> tuple[str, list[object]]:
        local_now = effective_at.astimezone(self._timezone)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end = local_start + timedelta(days=1)
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
                        )
                    )
                ).all()
            )
            tasks = list(
                (
                    await session.scalars(
                        select(Task).where(
                            Task.status == TaskStatus.PENDING,
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
            if commitment.due_at is not None and commitment.due_at < effective_at:
                text = (
                    f"⚠️ התחייבות באיחור — {self._local_time(commitment.due_at)}"
                    f" — {commitment.summary}"
                )
            elif commitment.due_at is None:
                text = f"📌 התחייבות ללא תאריך — {commitment.summary}"
            else:
                text = (
                    f"⏰ התחייבות עד {self._local_time(commitment.due_at)} — {commitment.summary}"
                )
            entries.append((commitment.due_at, text))
        for task in tasks:
            text = (
                f"📌 משימה ללא תאריך — {task.title}"
                if task.due_at is None
                else f"☑️ משימה עד {self._local_time(task.due_at)} — {task.title}"
            )
            if task.due_at is not None and task.due_at < effective_at:
                text = f"⚠️ משימה באיחור — {self._local_time(task.due_at)} — {task.title}"
            entries.append((task.due_at, text))
        entries.sort(key=lambda entry: entry[0] or utc_end)

        lines = ["🌤️ בוקר טוב", "━━━━━━━━━━━━", "📅 אירועי היום וכל המשימות וההתחייבויות הפתוחות:"]
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
        lines.extend(
            [
                "\n❓ בדיקת בוקר:",
                "• אילו משימות או התחייבויות כבר ביצעת? כתוב את שמותיהן כדי לסמן אותן.",
                "• מה הכי חשוב לך לקדם היום?",
            ]
        )
        open_items: list[Commitment | Task] = [*commitments, *tasks]
        if any(item.due_at is None for item in open_items):
            lines.append("• אילו תאריכים ושעות לקבוע לפריטים ללא תאריך?")
        if any(item.due_at is not None and item.due_at < effective_at for item in open_items):
            lines.append("• מה לעשות עם הפריטים שבאיחור: לבצע היום, לדחות או לבטל?")
        if any(item.due_at is not None for item in open_items):
            lines.append("• אילו פריטים מתוזמנים צריך לדחות, ולאיזה תאריך ושעה?")
        if calendar_unavailable:
            lines.append("\n📵 Google Calendar אינו זמין כרגע; הסיכום הפנימי נשמר.")
        return "\n".join(lines), surfaced_ids

    def _local_time(self, value: datetime | None) -> str:
        if value is None:
            return "ללא שעה"
        return value.astimezone(self._timezone).strftime("%d.%m.%Y %H:%M")

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
