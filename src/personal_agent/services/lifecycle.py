import asyncio
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.core.time import require_aware
from personal_agent.domain.enums import (
    ApprovalStatus,
    CalendarActionStatus,
    CommitmentStatus,
    ReminderKind,
    ReminderStatus,
    TaskStatus,
)
from personal_agent.domain.models import (
    ApprovalRequest,
    AuditLog,
    CalendarAction,
    Commitment,
    Reminder,
    Task,
)
from personal_agent.domain.schemas import CalendarProposal, CommitmentExtraction
from personal_agent.integrations.google_calendar.base import CalendarEvent, CalendarProvider
from personal_agent.integrations.telegram.base import TelegramNotifier

COMMITMENT_WORKFLOW_ACTION = "execute_commitment_workflow"
CLARIFICATION_ACTION = "clarify_extraction"
EXTRACTION_CONFIRMATION_ACTION = "confirm_extraction"
MAX_REMINDER_LEAD_MINUTES = 7 * 24 * 60


class LifecycleService:
    """Executes an extracted item and all durable downstream records exactly once."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: TelegramNotifier,
        calendar_provider: CalendarProvider | None,
        now: Callable[[], datetime],
        default_reminder_lead_minutes: int,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier
        self._calendar_provider = calendar_provider
        self._now = now
        self._default_reminder_lead_minutes = default_reminder_lead_minutes
        self._lock = asyncio.Lock()

    async def execute_approval(
        self,
        approval_id: uuid.UUID,
        *,
        resolution_source: str,
        due_at: datetime | None = None,
        effective_at: datetime | None = None,
    ) -> bool:
        executed_at = require_aware(effective_at or self._now())
        async with self._lock, self._session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if approval is None or approval.status is not ApprovalStatus.PENDING:
                return False
            if approval.expires_at is not None and approval.expires_at <= executed_at:
                approval.status = ApprovalStatus.EXPIRED
                approval.resolved_at = executed_at
                await session.commit()
                return False
            try:
                item = CommitmentExtraction.model_validate(approval.action_payload["item"])
                if due_at is not None:
                    item = item.model_copy(
                        update={
                            "due_at": require_aware(due_at),
                            "ambiguous": False,
                            "needs_clarification": False,
                            "requires_user_confirmation": False,
                        }
                    )
                self.validate_item(item, executed_at, allow_untimed=True)
                target_id = await self._materialize_item(
                    session, approval, item, resolution_source, executed_at
                )
                approval.status = ApprovalStatus.EXECUTED
                approval.resolved_at = executed_at
                session.add(
                    AuditLog(
                        actor="lifecycle_service",
                        action="execute_extraction_workflow",
                        target=str(target_id),
                        policy_decision=approval.risk_class,
                        source_event_id=approval.source_event_id,
                        approval_id=approval.id,
                        result="executed",
                        redacted_metadata={"resolution_source": resolution_source},
                    )
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                stored = await session.get(ApprovalRequest, approval_id)
                if stored is not None and stored.status is ApprovalStatus.PENDING:
                    stored.status = ApprovalStatus.FAILED
                    stored.resolved_at = executed_at
                    session.add(
                        AuditLog(
                            actor="lifecycle_service",
                            action="execute_extraction_workflow",
                            target=str(approval_id),
                            policy_decision=stored.risk_class,
                            source_event_id=stored.source_event_id,
                            approval_id=stored.id,
                            result="failed",
                            redacted_metadata={"error_type": type(exc).__name__},
                        )
                    )
                    await session.commit()
                await self._notifier.send_text(
                    "לא הצלחתי להשלים את שמירת הפריט. הבקשה נשמרה ככישלון בטוח."
                )
                raise

        confirmation = self._confirmation_text(item)
        if (
            item.calendar_event is not None or item.timetable_rows
        ) and self._calendar_provider is None:
            confirmation += "\nהתזכורת הפנימית נשמרה, אך Google Calendar אינו מחובר."  # noqa: RUF001
        await self._notifier.workflow_confirmation(approval.telegram_message_id, confirmation)
        return True

    @staticmethod
    def validate_item(item: CommitmentExtraction, now: datetime, *, allow_untimed: bool) -> None:
        if not item.summary.strip() or not item.evidence.strip():
            raise ValueError("Extraction summary and evidence are required")
        if item.due_at is None and not allow_untimed:
            raise ValueError("A due time is required")
        if item.due_at is not None and item.due_at <= now:
            raise ValueError("Due time must be in the future")
        proposals: list[CalendarProposal] = [*item.timetable_rows]
        if item.calendar_event is not None:
            proposals.append(item.calendar_event)
        for proposal in proposals:
            if proposal.end <= proposal.start:
                raise ValueError("Calendar event end must be after start")
            if proposal.attendees:
                raise ValueError("Attendee invitations are not supported")
            if any(
                not rule.startswith("RRULE:") or len(rule) > 500 for rule in proposal.recurrence
            ):
                raise ValueError("Calendar recurrence must contain bounded RRULE values")
        if item.calendar_worthy and item.calendar_event is None and not item.timetable_rows:
            raise ValueError("Calendar-worthy extraction requires a calendar event payload")

    async def _materialize_item(
        self,
        session: AsyncSession,
        approval: ApprovalRequest,
        item: CommitmentExtraction,
        resolution_source: str,
        executed_at: datetime,
    ) -> uuid.UUID:
        dedupe_key = str(approval.action_payload["item_dedupe_key"])
        if item.kind == "task":
            task = await session.scalar(select(Task).where(Task.dedupe_key == dedupe_key))
            if task is None:
                task = Task(
                    title=item.summary,
                    description=item.evidence,
                    status=TaskStatus.PENDING,
                    due_at=item.due_at,
                    source_event_id=approval.source_event_id,
                    confidence=item.confidence,
                    dedupe_key=dedupe_key,
                )
                session.add(task)
                await session.flush()
            else:
                task.title = item.summary
                task.description = item.evidence
                task.due_at = item.due_at
            await self._create_task_reminders(session, task, item, executed_at)
            task_actions = await self._create_task_calendar_actions(
                session, task, item, approval.id
            )
            for action, proposal in task_actions:
                await self._execute_calendar_action(action, proposal, executed_at)
            return task.id

        commitment = await session.scalar(
            select(Commitment).where(Commitment.dedupe_key == dedupe_key)
        )
        if commitment is None:
            if approval.source_event_id is None:
                raise ValueError("Commitment workflow requires a source event")
            commitment = Commitment(
                direction=item.direction,
                action_type=item.action_type,
                summary=item.summary,
                due_at=item.due_at,
                source_event_id=approval.source_event_id,
                source_approval_id=approval.id,
                status=CommitmentStatus.SCHEDULED,
                confidence=item.confidence,
                reminder_lead_minutes=self._default_reminder_lead_minutes,
                resolution_source=resolution_source,
                dedupe_key=dedupe_key,
            )
            session.add(commitment)
            await session.flush()
        else:
            commitment.due_at = item.due_at
            commitment.status = CommitmentStatus.SCHEDULED
            commitment.source_approval_id = approval.id
            commitment.resolution_source = resolution_source

        await self._create_reminders(session, commitment, item, executed_at)
        calendar_actions = await self._create_calendar_actions(session, commitment, item)
        for action, proposal in calendar_actions:
            await self._execute_calendar_action(action, proposal, executed_at)
            if commitment.calendar_action_id is None:
                commitment.calendar_action_id = action.id
        return commitment.id

    async def _create_reminders(
        self,
        session: AsyncSession,
        commitment: Commitment,
        item: CommitmentExtraction,
        executed_at: datetime,
    ) -> None:
        if item.due_at is None:
            return
        for lead in self._validated_reminder_leads(item):
            scheduled_for = item.due_at - timedelta(minutes=lead)
            kind = ReminderKind.DUE if lead == 0 else ReminderKind.BEFORE_DUE
            dedupe_key = f"{commitment.dedupe_key}:reminder:{lead}"
            existing = await session.scalar(
                select(Reminder).where(Reminder.dedupe_key == dedupe_key)
            )
            if existing is None:
                session.add(
                    Reminder(
                        commitment_id=commitment.id,
                        kind=kind,
                        scheduled_for=scheduled_for,
                        status=ReminderStatus.PENDING,
                        dedupe_key=dedupe_key,
                    )
                )
        if item.due_at <= executed_at:
            raise ValueError("Due time elapsed before workflow execution")

    async def _create_task_reminders(
        self,
        session: AsyncSession,
        task: Task,
        item: CommitmentExtraction,
        executed_at: datetime,
    ) -> None:
        if item.due_at is None:
            return
        for lead in self._validated_reminder_leads(item):
            dedupe_key = f"{task.dedupe_key}:reminder:{lead}"
            existing = await session.scalar(
                select(Reminder).where(Reminder.dedupe_key == dedupe_key)
            )
            if existing is None:
                session.add(
                    Reminder(
                        task_id=task.id,
                        kind=ReminderKind.DUE if lead == 0 else ReminderKind.BEFORE_DUE,
                        scheduled_for=item.due_at - timedelta(minutes=lead),
                        status=ReminderStatus.PENDING,
                        dedupe_key=dedupe_key,
                    )
                )
        if item.due_at <= executed_at:
            raise ValueError("Task due time elapsed before workflow execution")

    def _validated_reminder_leads(self, item: CommitmentExtraction) -> list[int]:
        proposed = item.reminder_lead_minutes
        if (
            proposed
            and len(proposed) <= 4
            and all(0 <= lead <= MAX_REMINDER_LEAD_MINUTES for lead in proposed)
        ):
            leads = proposed
        elif item.assignment_deadline:
            leads = [24 * 60, 120]
        elif item.calendar_worthy:
            leads = [30, 10]
        else:
            leads = [self._default_reminder_lead_minutes, 0]
        if not item.calendar_worthy and 0 not in leads:
            leads = [*leads, 0]
        return sorted(set(leads), reverse=True)

    async def _create_calendar_actions(
        self,
        session: AsyncSession,
        commitment: Commitment,
        item: CommitmentExtraction,
    ) -> list[tuple[CalendarAction, CalendarProposal]]:
        proposals: list[CalendarProposal] = [row for row in item.timetable_rows if row.included]
        if item.calendar_event is not None:
            proposals.append(item.calendar_event)
        results: list[tuple[CalendarAction, CalendarProposal]] = []
        for index, proposal in enumerate(proposals):
            dedupe_key = f"{commitment.dedupe_key}:calendar:{index}"
            action = await session.scalar(
                select(CalendarAction).where(CalendarAction.dedupe_key == dedupe_key)
            )
            if action is None:
                action = CalendarAction(
                    source_event_id=commitment.source_event_id,
                    commitment_id=commitment.id,
                    approval_id=commitment.source_approval_id,
                    operation="create",
                    payload_json=proposal.model_dump(mode="json"),
                    status=(
                        CalendarActionStatus.PENDING
                        if self._calendar_provider is not None
                        else CalendarActionStatus.PENDING_CONFIGURATION
                    ),
                    dedupe_key=dedupe_key,
                )
                session.add(action)
                await session.flush()
            results.append((action, proposal))
            if action.approval_id is None:
                action.approval_id = commitment.source_approval_id
        return results

    async def _create_task_calendar_actions(
        self,
        session: AsyncSession,
        task: Task,
        item: CommitmentExtraction,
        approval_id: uuid.UUID,
    ) -> list[tuple[CalendarAction, CalendarProposal]]:
        if task.source_event_id is None:
            return []
        proposals: list[CalendarProposal] = [row for row in item.timetable_rows if row.included]
        if item.calendar_event is not None:
            proposals.append(item.calendar_event)
        results: list[tuple[CalendarAction, CalendarProposal]] = []
        for index, proposal in enumerate(proposals):
            dedupe_key = f"{task.dedupe_key}:calendar:{index}"
            action = await session.scalar(
                select(CalendarAction).where(CalendarAction.dedupe_key == dedupe_key)
            )
            if action is None:
                payload = proposal.model_dump(mode="json")
                payload["task_id"] = str(task.id)
                action = CalendarAction(
                    source_event_id=task.source_event_id,
                    approval_id=approval_id,
                    operation="create",
                    payload_json=payload,
                    status=(
                        CalendarActionStatus.PENDING
                        if self._calendar_provider is not None
                        else CalendarActionStatus.PENDING_CONFIGURATION
                    ),
                    dedupe_key=dedupe_key,
                )
                session.add(action)
                await session.flush()
            results.append((action, proposal))
            if action.approval_id is None:
                action.approval_id = approval_id
        return results

    async def stage_calendar_actions(
        self,
        session: AsyncSession,
        commitment: Commitment,
        item: CommitmentExtraction,
    ) -> int:
        """Persist previews during the grace period without performing a Calendar write."""
        actions = await self._create_calendar_actions(session, commitment, item)
        if actions and commitment.calendar_action_id is None:
            commitment.calendar_action_id = actions[0][0].id
        return len(actions)

    async def stage_task_calendar_actions(
        self,
        session: AsyncSession,
        task: Task,
        item: CommitmentExtraction,
        approval_id: uuid.UUID,
    ) -> int:
        actions = await self._create_task_calendar_actions(session, task, item, approval_id)
        return len(actions)

    async def recover_configured_calendar_actions(self, at: datetime | None = None) -> int:
        """Replay approved Calendar creates that were blocked only by missing OAuth config."""
        if self._calendar_provider is None:
            return 0
        executed_at = require_aware(at or self._now())
        async with self._lock, self._session_factory() as session:
            actions = list(
                (
                    await session.scalars(
                        select(CalendarAction)
                        .join(
                            ApprovalRequest,
                            ApprovalRequest.id == CalendarAction.approval_id,
                        )
                        .where(
                            CalendarAction.status == CalendarActionStatus.PENDING_CONFIGURATION,
                            ApprovalRequest.status == ApprovalStatus.EXECUTED,
                        )
                    )
                ).all()
            )
            recovered = 0
            for action in actions:
                proposal_payload = {
                    key: value for key, value in action.payload_json.items() if key != "task_id"
                }
                proposal = CalendarProposal.model_validate(proposal_payload)
                try:
                    await self._execute_calendar_action(action, proposal, executed_at)
                except Exception as exc:
                    action.status = CalendarActionStatus.FAILED
                    session.add(
                        AuditLog(
                            actor="lifecycle_service",
                            action="recover_calendar_action",
                            target=str(action.id),
                            approval_id=action.approval_id,
                            result="failed",
                            redacted_metadata={"error_type": type(exc).__name__},
                        )
                    )
                    continue
                recovered += 1
                session.add(
                    AuditLog(
                        actor="lifecycle_service",
                        action="recover_calendar_action",
                        target=str(action.id),
                        approval_id=action.approval_id,
                        result="executed",
                    )
                )
            await session.commit()
            return recovered

    async def _execute_calendar_action(
        self, action: CalendarAction, proposal: CalendarProposal, executed_at: datetime
    ) -> None:
        if action.status is CalendarActionStatus.EXECUTED or self._calendar_provider is None:
            return
        external_id = f"pa{action.id.hex}"
        created = await self._calendar_provider.create_event(
            CalendarEvent(
                external_id=external_id,
                summary=proposal.summary,
                description=proposal.description,
                start=proposal.start,
                end=proposal.end,
                recurrence=proposal.recurrence,
            )
        )
        action.google_event_id = created.external_id
        action.google_recurrence_id = created.external_id if proposal.recurrence else None
        action.status = CalendarActionStatus.EXECUTED
        action.executed_at = executed_at

    @staticmethod
    def _confirmation_text(item: CommitmentExtraction) -> str:
        if item.kind == "task":
            return f"✅ המשימה נשמרה\n━━━━━━━━━━━━\n📝 {item.summary}"
        if item.due_at is None:
            return f"✅ ההתחייבות נשמרה\n━━━━━━━━━━━━\n📝 {item.summary}\n🕓 ללא שעה עדיין"
        return f"✅ ההתחייבות נשמרה\n━━━━━━━━━━━━\n📝 {item.summary}\n🔔 נוצרו תזכורות עמידות."


def is_valid_proposed_time(
    proposed_at: datetime,
    now: datetime,
    timezone: str,
    quiet_hours_start: time | None,
    quiet_hours_end: time | None,
    *,
    explicit_date: date | None = None,
) -> bool:
    proposed_at = require_aware(proposed_at)
    now = require_aware(now)
    if proposed_at <= now:
        return False
    if explicit_date is not None:
        local_zone = ZoneInfo(timezone)
        if proposed_at.astimezone(local_zone).date() != explicit_date:
            return False
    if quiet_hours_start is None or quiet_hours_end is None:
        return True
    local_time = proposed_at.astimezone(ZoneInfo(timezone)).time().replace(tzinfo=None)
    if quiet_hours_start <= quiet_hours_end:
        return not (quiet_hours_start <= local_time < quiet_hours_end)
    return not (local_time >= quiet_hours_start or local_time < quiet_hours_end)
