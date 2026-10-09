import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.core.time import require_aware
from personal_agent.domain.enums import (
    ApprovalStatus,
    CommitmentStatus,
    ReminderKind,
    ReminderStatus,
    TaskStatus,
)
from personal_agent.domain.models import (
    ApprovalRequest,
    AuditLog,
    Commitment,
    Event,
    Reminder,
    Task,
)
from personal_agent.domain.schemas import CommitmentExtraction, TimeProposalRequest
from personal_agent.integrations.llm.base import LLMProvider
from personal_agent.integrations.telegram.base import TelegramNotifier
from personal_agent.integrations.telegram.presentation import friendly_local_datetime
from personal_agent.services.lifecycle import (
    CLARIFICATION_ACTION,
    COMMITMENT_WORKFLOW_ACTION,
    LifecycleService,
    is_valid_proposed_time,
)

logger = logging.getLogger(__name__)


class ReminderService:
    """Recovers and executes persisted timers using idempotent database transitions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: TelegramNotifier,
        lifecycle: LifecycleService,
        llm_provider: LLMProvider,
        now: Callable[[], datetime],
        *,
        timezone: str = "Asia/Jerusalem",
        quiet_hours_start: time | None = None,
        quiet_hours_end: time | None = None,
        overdue_grace_minutes: int = 15,
        default_reminder_lead_minutes: int = 5,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier
        self._lifecycle = lifecycle
        self._llm_provider = llm_provider
        self._now = now
        self._timezone = timezone
        self._quiet_hours_start = quiet_hours_start
        self._quiet_hours_end = quiet_hours_end
        self._overdue_grace_minutes = overdue_grace_minutes
        self._default_reminder_lead_minutes = default_reminder_lead_minutes
        self._lock = asyncio.Lock()

    async def cancel_pending_action(
        self, approval_id: uuid.UUID, at: datetime | None = None
    ) -> bool:
        effective_at = require_aware(at or self._now())
        async with self._lock, self._session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if approval is None or approval.status is not ApprovalStatus.PENDING:
                return False
            approval.status = ApprovalStatus.REJECTED
            approval.resolved_at = effective_at
            commitment_id = approval.action_payload.get("commitment_id")
            if commitment_id is not None:
                commitment = await session.get(Commitment, uuid.UUID(str(commitment_id)))
                if commitment is not None:
                    commitment.status = CommitmentStatus.CANCELLED
            session.add(
                AuditLog(
                    actor="user",
                    action="cancel_pending_workflow",
                    target=str(commitment_id or approval.id),
                    policy_decision=approval.risk_class,
                    source_event_id=approval.source_event_id,
                    approval_id=approval.id,
                    result="cancelled_during_grace",
                )
            )
            await session.commit()
            return True

    async def execute_pending_action_now(
        self, approval_id: uuid.UUID, at: datetime | None = None
    ) -> bool:
        return await self._lifecycle.execute_approval(
            approval_id,
            resolution_source="user_confirmed",
            effective_at=at,
        )

    async def execute_due_internal_actions(self, at: datetime | None = None) -> int:
        effective_at = require_aware(at or self._now())
        async with self._session_factory() as session:
            statement = select(ApprovalRequest.id).where(
                ApprovalRequest.action_type == COMMITMENT_WORKFLOW_ACTION,
                ApprovalRequest.status == ApprovalStatus.PENDING,
                ApprovalRequest.execute_after <= effective_at,
            )
            approval_ids = list((await session.scalars(statement)).all())
        executed = 0
        for approval_id in approval_ids:
            if await self._lifecycle.execute_approval(
                approval_id,
                resolution_source="high_confidence_grace",
                effective_at=effective_at,
            ):
                executed += 1
        return executed

    async def resolve_due_clarifications(self, at: datetime | None = None) -> int:
        effective_at = require_aware(at or self._now())
        async with self._session_factory() as session:
            statement = select(ApprovalRequest).where(
                ApprovalRequest.action_type == CLARIFICATION_ACTION,
                ApprovalRequest.status == ApprovalStatus.PENDING,
                ApprovalRequest.execute_after <= effective_at,
            )
            approvals = list((await session.scalars(statement)).all())
        resolved = 0
        for approval in approvals:
            item = CommitmentExtraction.model_validate(approval.action_payload["item"])
            try:
                proposal = await self._llm_provider.propose_time(
                    TimeProposalRequest(
                        purpose="clarification_fallback",
                        summary=item.summary,
                        source_text=str(approval.action_payload.get("source_text", "")),
                        reference_at=effective_at,
                        explicit_date=item.explicit_date,
                    )
                )
            except Exception as exc:
                await self._record_fallback_failure(approval.id, effective_at, type(exc).__name__)
                continue
            if not is_valid_proposed_time(
                proposal.proposed_at,
                effective_at,
                self._timezone,
                self._quiet_hours_start,
                self._quiet_hours_end,
                explicit_date=item.explicit_date,
            ):
                async with self._session_factory() as session:
                    stored = await session.get(ApprovalRequest, approval.id)
                    if stored is not None and stored.status is ApprovalStatus.PENDING:
                        stored.execute_after = None
                        stored.action_payload = {
                            **stored.action_payload,
                            "fallback_error": "invalid_time_proposal",
                        }
                        session.add(
                            AuditLog(
                                actor="reminder_service",
                                action="validate_clarification_fallback",
                                target=str(stored.id),
                                approval_id=stored.id,
                                result="rejected_invalid_proposal",
                            )
                        )
                        await session.commit()
                continue
            try:
                handled = await self._lifecycle.execute_approval(
                    approval.id,
                    resolution_source="llm_fallback",
                    due_at=proposal.proposed_at,
                    effective_at=effective_at,
                )
            except Exception:
                logger.exception("Clarification fallback execution failed: %s", approval.id)
                continue
            if handled:
                await self._notifier.send_text(
                    "לא נבחרה שעה, לכן קבעתי שעה לפי ההקשר: "
                    f"{proposal.proposed_at.isoformat()}. אפשר לשנות או לבטל."
                )
                resolved += 1
        return resolved

    async def _record_fallback_failure(
        self, approval_id: uuid.UUID, effective_at: datetime, error_type: str
    ) -> None:
        async with self._session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if approval is None or approval.status is not ApprovalStatus.PENDING:
                return
            attempts = int(approval.action_payload.get("fallback_attempts", 0)) + 1
            approval.action_payload = {
                **approval.action_payload,
                "fallback_attempts": attempts,
                "fallback_error": error_type,
            }
            approval.execute_after = effective_at + timedelta(minutes=5) if attempts < 3 else None
            session.add(
                AuditLog(
                    actor="reminder_service",
                    action="propose_clarification_fallback",
                    target=str(approval.id),
                    approval_id=approval.id,
                    result="retry_scheduled" if attempts < 3 else "awaiting_user_input",
                    redacted_metadata={"error_type": error_type, "attempt": attempts},
                )
            )
            await session.commit()

    async def dispatch_due_reminders(self, at: datetime | None = None) -> int:
        effective_at = require_aware(at or self._now())
        async with self._lock, self._session_factory() as session:
            reminders = list(
                (
                    await session.scalars(
                        select(Reminder).where(
                            Reminder.status == ReminderStatus.PENDING,
                            Reminder.scheduled_for <= effective_at,
                        )
                    )
                ).all()
            )
            delivered = 0
            for reminder in reminders:
                commitment = (
                    await session.get(Commitment, reminder.commitment_id)
                    if reminder.commitment_id is not None
                    else None
                )
                task = (
                    await session.get(Task, reminder.task_id)
                    if reminder.task_id is not None
                    else None
                )
                commitment_closed = commitment is not None and commitment.status in {
                    CommitmentStatus.DONE,
                    CommitmentStatus.CANCELLED,
                }
                task_closed = task is not None and task.status in {
                    TaskStatus.DONE,
                    TaskStatus.CANCELLED,
                }
                if (commitment is None and task is None) or commitment_closed or task_closed:
                    reminder.status = ReminderStatus.CANCELLED
                    continue
                if commitment is not None:
                    target_id = commitment.id
                    summary = commitment.summary
                    due_at = commitment.due_at
                else:
                    assert task is not None
                    target_id = task.id
                    summary = task.title
                    due_at = task.due_at
                reminder.status = ReminderStatus.SENT
                reminder.sent_at = effective_at
                session.add(
                    AuditLog(
                        actor="reminder_service",
                        action="claim_reminder_delivery",
                        target=str(target_id),
                        result="delivery_claimed",
                        redacted_metadata={"reminder_id": str(reminder.id)},
                    )
                )
                await session.commit()
                try:
                    message_id = await self._notifier.reminder(
                        str(target_id),
                        summary,
                        due_at or reminder.scheduled_for,
                        str(reminder.id),
                    )
                except Exception as exc:
                    # A transient Telegram failure must not permanently consume the reminder.
                    # Return it to the durable queue so the next scheduler pass retries it.
                    reminder.status = ReminderStatus.PENDING
                    reminder.sent_at = None
                    session.add(
                        AuditLog(
                            actor="reminder_service",
                            action="send_reminder_to_user",
                            target=str(target_id),
                            result="delivery_failed_retry_pending",
                            redacted_metadata={
                                "reminder_id": str(reminder.id),
                                "error_type": type(exc).__name__,
                            },
                        )
                    )
                    await session.commit()
                    logger.warning(
                        "reminder_delivery_failed_retry_pending",
                        extra={
                            "reminder_id": str(reminder.id),
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
                reminder.telegram_message_id = message_id
                delivered += 1
                if reminder.kind is ReminderKind.DUE and self._overdue_grace_minutes > 0:
                    follow_up_key = f"{reminder.dedupe_key}:unanswered-follow-up"
                    follow_up = await session.scalar(
                        select(Reminder).where(Reminder.dedupe_key == follow_up_key)
                    )
                    if follow_up is None:
                        session.add(
                            Reminder(
                                commitment_id=reminder.commitment_id,
                                task_id=reminder.task_id,
                                kind=ReminderKind.SNOOZE,
                                scheduled_for=effective_at
                                + timedelta(minutes=self._overdue_grace_minutes),
                                status=ReminderStatus.PENDING,
                                dedupe_key=follow_up_key,
                            )
                        )
                session.add(
                    AuditLog(
                        actor="reminder_service",
                        action="send_reminder_to_user",
                        target=str(target_id),
                        result="notified",
                        redacted_metadata={"reminder_id": str(reminder.id)},
                    )
                )
            await session.commit()
            return delivered

    async def mark_overdue(self, at: datetime | None = None) -> int:
        effective_at = require_aware(at or self._now())
        cutoff = effective_at - timedelta(minutes=self._overdue_grace_minutes)
        async with self._lock, self._session_factory() as session:
            commitments = list(
                (
                    await session.scalars(
                        select(Commitment).where(
                            Commitment.status == CommitmentStatus.SCHEDULED,
                            Commitment.due_at.is_not(None),
                            Commitment.due_at <= cutoff,
                            Commitment.overdue_at.is_(None),
                        )
                    )
                ).all()
            )
            for commitment in commitments:
                commitment.status = CommitmentStatus.OVERDUE
                commitment.overdue_at = effective_at
                await self._notifier.send_text(f"התחייבות באיחור: {commitment.summary}")
                session.add(
                    AuditLog(
                        actor="reminder_service",
                        action="mark_commitment_overdue",
                        target=str(commitment.id),
                        result="notified_once",
                    )
                )
            await session.commit()
            return len(commitments)

    async def expire_approvals(self, at: datetime | None = None) -> int:
        effective_at = require_aware(at or self._now())
        async with self._lock, self._session_factory() as session:
            approvals = list(
                (
                    await session.scalars(
                        select(ApprovalRequest).where(
                            ApprovalRequest.status == ApprovalStatus.PENDING,
                            ApprovalRequest.expires_at.is_not(None),
                            ApprovalRequest.expires_at <= effective_at,
                        )
                    )
                ).all()
            )
            for approval in approvals:
                approval.status = ApprovalStatus.EXPIRED
                approval.resolved_at = effective_at
            await session.commit()
            return len(approvals)

    async def mark_done(self, commitment_id: uuid.UUID, at: datetime | None = None) -> bool:
        return await self._resolve_item(commitment_id, done=True, at=at)

    async def cancel_commitment(self, commitment_id: uuid.UUID, at: datetime | None = None) -> bool:
        return await self._resolve_item(commitment_id, done=False, at=at)

    async def _resolve_item(self, item_id: uuid.UUID, *, done: bool, at: datetime | None) -> bool:
        effective_at = require_aware(at or self._now())
        async with self._lock, self._session_factory() as session:
            commitment = await session.get(Commitment, item_id)
            task = await session.get(Task, item_id) if commitment is None else None
            if commitment is None and task is None:
                return False
            if commitment is not None:
                if commitment.status in {CommitmentStatus.DONE, CommitmentStatus.CANCELLED}:
                    return False
                commitment.status = CommitmentStatus.DONE if done else CommitmentStatus.CANCELLED
            elif task is not None:
                if task.status in {TaskStatus.DONE, TaskStatus.CANCELLED}:
                    return False
                task.status = TaskStatus.DONE if done else TaskStatus.CANCELLED
            reminders = list(
                (
                    await session.scalars(
                        select(Reminder).where(
                            ((Reminder.commitment_id == item_id) | (Reminder.task_id == item_id)),
                            Reminder.status.in_([ReminderStatus.PENDING, ReminderStatus.SENT]),
                        )
                    )
                ).all()
            )
            for reminder in reminders:
                reminder.status = ReminderStatus.HANDLED
            session.add(
                AuditLog(
                    actor="user",
                    action=f"mark_item_{'done' if done else 'cancelled'}",
                    target=str(item_id),
                    result="executed",
                    timestamp=effective_at,
                )
            )
            await session.commit()
            return True

    async def smart_snooze(self, reminder_id: uuid.UUID, at: datetime | None = None) -> bool:
        effective_at = require_aware(at or self._now())
        async with self._session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            if reminder is None or reminder.status not in {
                ReminderStatus.PENDING,
                ReminderStatus.SENT,
            }:
                return False
            commitment = (
                await session.get(Commitment, reminder.commitment_id)
                if reminder.commitment_id is not None
                else None
            )
            task = (
                await session.get(Task, reminder.task_id) if reminder.task_id is not None else None
            )
            if commitment is None and task is None:
                return False
            source_event_id: uuid.UUID | None
            if commitment is not None:
                source_event_id = commitment.source_event_id
                summary = commitment.summary
            else:
                assert task is not None
                source_event_id = task.source_event_id
                summary = task.title
            event = (
                await session.get(Event, source_event_id) if source_event_id is not None else None
            )
            source_text = event.content_text if event is not None else ""
        proposal = await self._llm_provider.propose_time(
            TimeProposalRequest(
                purpose="smart_snooze",
                summary=summary,
                source_text=source_text or "",
                reference_at=effective_at,
            )
        )
        if not is_valid_proposed_time(
            proposal.proposed_at,
            effective_at,
            self._timezone,
            self._quiet_hours_start,
            self._quiet_hours_end,
        ):
            return False
        async with self._lock, self._session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            if reminder is None or reminder.status not in {
                ReminderStatus.PENDING,
                ReminderStatus.SENT,
            }:
                return False
            target_filter = (
                Reminder.commitment_id == reminder.commitment_id
                if reminder.commitment_id is not None
                else Reminder.task_id == reminder.task_id
            )
            active_reminders = list(
                (
                    await session.scalars(
                        select(Reminder).where(
                            target_filter,
                            Reminder.status.in_([ReminderStatus.PENDING, ReminderStatus.SENT]),
                        )
                    )
                ).all()
            )
            for active_reminder in active_reminders:
                active_reminder.status = (
                    ReminderStatus.HANDLED
                    if active_reminder.status is ReminderStatus.SENT
                    else ReminderStatus.CANCELLED
                )
            replacement_key = f"{reminder.dedupe_key}:snooze:{proposal.proposed_at.isoformat()}"
            existing = await session.scalar(
                select(Reminder).where(Reminder.dedupe_key == replacement_key)
            )
            if existing is None:
                session.add(
                    Reminder(
                        commitment_id=reminder.commitment_id,
                        task_id=reminder.task_id,
                        kind=ReminderKind.SNOOZE,
                        scheduled_for=proposal.proposed_at,
                        status=ReminderStatus.PENDING,
                        dedupe_key=replacement_key,
                    )
                )
            session.add(
                AuditLog(
                    actor="reminder_service",
                    action="smart_snooze",
                    target=str(reminder.commitment_id or reminder.task_id),
                    result="scheduled",
                    redacted_metadata={
                        "scheduled_for": proposal.proposed_at.isoformat(),
                        "decision_summary": proposal.decision_summary,
                    },
                )
            )
            await session.commit()
        friendly_time = friendly_local_datetime(
            proposal.proposed_at,
            effective_at,
            ZoneInfo(self._timezone),
        )
        await self._notifier.send_text(f"✅ מעולה, אזכיר לך {friendly_time}.")
        return True

    async def snooze_for(
        self,
        reminder_id: uuid.UUID,
        minutes: int,
        at: datetime | None = None,
    ) -> bool:
        if minutes not in {10, 60}:
            return False
        effective_at = require_aware(at or self._now())
        scheduled_for = effective_at + timedelta(minutes=minutes)
        async with self._lock, self._session_factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            if reminder is None or reminder.status not in {
                ReminderStatus.PENDING,
                ReminderStatus.SENT,
            }:
                return False
            target_filter = (
                Reminder.commitment_id == reminder.commitment_id
                if reminder.commitment_id is not None
                else Reminder.task_id == reminder.task_id
            )
            active_reminders = list(
                (
                    await session.scalars(
                        select(Reminder).where(
                            target_filter,
                            Reminder.status.in_([ReminderStatus.PENDING, ReminderStatus.SENT]),
                        )
                    )
                ).all()
            )
            for active_reminder in active_reminders:
                active_reminder.status = (
                    ReminderStatus.HANDLED
                    if active_reminder.status is ReminderStatus.SENT
                    else ReminderStatus.CANCELLED
                )
            dedupe_key = f"{reminder.dedupe_key}:quick:{minutes}"
            existing = await session.scalar(
                select(Reminder).where(Reminder.dedupe_key == dedupe_key)
            )
            if existing is None:
                session.add(
                    Reminder(
                        commitment_id=reminder.commitment_id,
                        task_id=reminder.task_id,
                        kind=ReminderKind.SNOOZE,
                        scheduled_for=scheduled_for,
                        status=ReminderStatus.PENDING,
                        dedupe_key=dedupe_key,
                    )
                )
            session.add(
                AuditLog(
                    actor="user",
                    action="quick_snooze",
                    target=str(reminder.commitment_id or reminder.task_id),
                    result="scheduled",
                    redacted_metadata={"minutes": minutes},
                )
            )
            await session.commit()
            return True

    async def reschedule(
        self,
        commitment_id: uuid.UUID,
        due_at: datetime,
        at: datetime | None = None,
    ) -> bool:
        effective_at = require_aware(at or self._now())
        due_at = require_aware(due_at)
        if due_at <= effective_at:
            return False
        async with self._lock, self._session_factory() as session:
            commitment = await session.get(Commitment, commitment_id)
            task = await session.get(Task, commitment_id) if commitment is None else None
            if commitment is None and task is None:
                return False
            if commitment is not None and commitment.status in {
                CommitmentStatus.DONE,
                CommitmentStatus.CANCELLED,
            }:
                return False
            if task is not None and task.status in {TaskStatus.DONE, TaskStatus.CANCELLED}:
                return False
            existing = list(
                (
                    await session.scalars(
                        select(Reminder).where(
                            (
                                (Reminder.commitment_id == commitment_id)
                                | (Reminder.task_id == commitment_id)
                            ),
                            Reminder.status.in_([ReminderStatus.PENDING, ReminderStatus.SENT]),
                        )
                    )
                ).all()
            )
            for reminder in existing:
                reminder.status = (
                    ReminderStatus.HANDLED
                    if reminder.status is ReminderStatus.SENT
                    else ReminderStatus.CANCELLED
                )
            if commitment is not None:
                commitment.due_at = due_at
                commitment.status = CommitmentStatus.SCHEDULED
                commitment.overdue_at = None
                dedupe_key = commitment.dedupe_key
            else:
                assert task is not None
                task.due_at = due_at
                dedupe_key = task.dedupe_key or f"task:{task.id}"
            version = due_at.isoformat()
            for lead, kind in (
                (self._default_reminder_lead_minutes, ReminderKind.BEFORE_DUE),
                (0, ReminderKind.DUE),
            ):
                session.add(
                    Reminder(
                        commitment_id=commitment.id if commitment is not None else None,
                        task_id=task.id if task is not None else None,
                        kind=kind,
                        scheduled_for=due_at - timedelta(minutes=lead),
                        status=ReminderStatus.PENDING,
                        dedupe_key=f"{dedupe_key}:reschedule:{version}:{lead}",
                    )
                )
            session.add(
                AuditLog(
                    actor="user",
                    action="reschedule_item",
                    target=str(commitment_id),
                    result="scheduled",
                    redacted_metadata={"due_at": due_at.isoformat()},
                )
            )
            await session.commit()
            return True
