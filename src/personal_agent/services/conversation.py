import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.core.time import require_aware
from personal_agent.domain.enums import (
    ActionClass,
    ApprovalStatus,
    CommitmentStatus,
    EventDirection,
    EventSource,
    MemoryStatus,
    ProcessingStatus,
    TaskStatus,
)
from personal_agent.domain.models import (
    ApprovalRequest,
    AuditLog,
    Commitment,
    Event,
    MemoryFact,
    Task,
)
from personal_agent.domain.schemas import ChatRequest, ChatResponse, ChatTurn, MemoryContext
from personal_agent.integrations.google_calendar.base import CalendarProvider
from personal_agent.integrations.llm.base import LLMProvider
from personal_agent.integrations.telegram.base import TelegramNotifier
from personal_agent.integrations.telegram.presentation import conversational_text

MEMORY_APPROVAL_ACTION = "confirm_memory_fact"
MEMORY_CONFIDENCE_THRESHOLD = 0.75
logger = logging.getLogger(__name__)


class ConversationService:
    """Provides bounded LLM chat and approval-gated structured personal memory."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        llm_provider: LLMProvider,
        notifier: TelegramNotifier,
        calendar_provider: CalendarProvider | None,
        now: Callable[[], datetime],
        timezone: str,
    ) -> None:
        self._session_factory = session_factory
        self._llm_provider = llm_provider
        self._notifier = notifier
        self._calendar_provider = calendar_provider
        self._now = now
        self._timezone = ZoneInfo(timezone)

    async def respond(
        self,
        source_event_id: uuid.UUID,
        conversation_id: str,
        message: str,
    ) -> ChatResponse:
        request = await self._build_request(conversation_id, message)
        response = await self._llm_provider.chat(request)
        effective_at = require_aware(self._now())
        proposals: list[tuple[ApprovalRequest, MemoryFact]] = []
        async with self._session_factory() as session:
            source_event = await session.get(Event, source_event_id)
            if source_event is None:
                raise ValueError("Chat source event was not found")
            reply_key = f"telegram:assistant_reply:{source_event.id}"
            existing_reply = await session.scalar(
                select(Event).where(
                    Event.source == EventSource.TELEGRAM,
                    Event.source_account == source_event.source_account,
                    Event.dedupe_key == reply_key,
                )
            )
            if existing_reply is not None:
                return response
            reply_event = Event(
                source=EventSource.TELEGRAM,
                source_account=source_event.source_account,
                external_id=f"assistant:{source_event.id}",
                event_type="assistant.reply",
                direction=EventDirection.OUTBOUND,
                occurred_at=effective_at,
                received_at=effective_at,
                actor_external_id="personal-agent",
                actor_display_name="AI assistant",
                conversation_external_id=conversation_id,
                content_text=response.reply,
                payload_json={"in_reply_to": str(source_event.id)},
                dedupe_key=reply_key,
                sensitivity=source_event.sensitivity,
                processing_status=ProcessingStatus.PROCESSED,
            )
            session.add(reply_event)
            for candidate in response.memory_candidates:
                if (
                    candidate.confidence < MEMORY_CONFIDENCE_THRESHOLD
                    or not self._is_durable_memory_candidate(candidate)
                ):
                    continue
                duplicate = await self._find_existing_memory(session, candidate)
                if duplicate is not None:
                    continue
                memory = MemoryFact(
                    category=candidate.category,
                    subject=candidate.subject,
                    predicate=candidate.predicate,
                    value_json={"value": candidate.value},
                    status=MemoryStatus.PROPOSED,
                    confidence=candidate.confidence,
                    sensitivity=candidate.sensitivity,
                    source_event_ids=[str(source_event.id)],
                    valid_from=effective_at,
                    last_verified_at=effective_at,
                )
                session.add(memory)
                await session.flush()
                approval = ApprovalRequest(
                    action_type=MEMORY_APPROVAL_ACTION,
                    action_payload={"memory_fact_id": str(memory.id)},
                    risk_class=ActionClass.INTERNAL_REVERSIBLE.value,
                    status=ApprovalStatus.PENDING,
                    source_event_id=source_event.id,
                    dedupe_key=f"memory:{memory.id}:approval",
                )
                session.add(approval)
                await session.flush()
                session.add(
                    AuditLog(
                        actor="conversation_service",
                        action="propose_memory_fact",
                        target=str(memory.id),
                        policy_decision="explicit_user_confirmation",
                        source_event_id=source_event.id,
                        approval_id=approval.id,
                        result="pending",
                    )
                )
                proposals.append((approval, memory))
            await session.commit()

        reply_message_id = await self._notifier.send_text(conversational_text(response.reply))
        for approval, memory in proposals:
            approval.telegram_message_id = await self._notifier.memory_request(
                str(approval.id),
                self._memory_summary(memory),
            )
        async with self._session_factory() as session:
            stored_reply = await session.scalar(
                select(Event).where(
                    Event.dedupe_key == f"telegram:assistant_reply:{source_event_id}"
                )
            )
            if stored_reply is not None:
                stored_reply.payload_json = {
                    **stored_reply.payload_json,
                    "telegram_message_id": reply_message_id,
                }
            for approval, _memory in proposals:
                stored_approval = await session.get(ApprovalRequest, approval.id)
                if stored_approval is not None:
                    stored_approval.telegram_message_id = approval.telegram_message_id
            await session.commit()
        return response

    async def resolve_memory(self, approval_id: uuid.UUID, *, remember: bool) -> bool:
        effective_at = require_aware(self._now())
        async with self._session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if (
                approval is None
                or approval.action_type != MEMORY_APPROVAL_ACTION
                or approval.status is not ApprovalStatus.PENDING
            ):
                return False
            memory_id = uuid.UUID(str(approval.action_payload["memory_fact_id"]))
            memory = await session.get(MemoryFact, memory_id)
            if memory is None or memory.status is not MemoryStatus.PROPOSED:
                return False
            memory.status = MemoryStatus.CONFIRMED if remember else MemoryStatus.REJECTED
            memory.last_verified_at = effective_at
            approval.status = ApprovalStatus.EXECUTED if remember else ApprovalStatus.REJECTED
            approval.resolved_at = effective_at
            session.add(
                AuditLog(
                    actor="telegram_user",
                    action="resolve_memory_fact",
                    target=str(memory.id),
                    policy_decision="explicit_user_decision",
                    source_event_id=approval.source_event_id,
                    approval_id=approval.id,
                    result="confirmed" if remember else "rejected",
                )
            )
            await session.commit()
            return True

    async def _build_request(self, conversation_id: str, message: str) -> ChatRequest:
        async with self._session_factory() as session:
            recent_events = list(
                (
                    await session.scalars(
                        select(Event)
                        .where(
                            Event.source == EventSource.TELEGRAM,
                            Event.conversation_external_id == conversation_id,
                            Event.content_text.is_not(None),
                        )
                        .order_by(Event.occurred_at.desc())
                        .limit(12)
                    )
                ).all()
            )
            memories = list(
                (
                    await session.scalars(
                        select(MemoryFact)
                        .where(MemoryFact.status == MemoryStatus.CONFIRMED)
                        .order_by(MemoryFact.last_verified_at.desc())
                        .limit(50)
                    )
                ).all()
            )
            commitments = list(
                (
                    await session.scalars(
                        select(Commitment)
                        .where(
                            Commitment.status.not_in(
                                [CommitmentStatus.DONE, CommitmentStatus.CANCELLED]
                            )
                        )
                        .order_by(Commitment.due_at.is_(None), Commitment.due_at)
                        .limit(20)
                    )
                ).all()
            )
            tasks = list(
                (
                    await session.scalars(
                        select(Task)
                        .where(Task.status == TaskStatus.PENDING)
                        .order_by(Task.due_at.is_(None), Task.due_at)
                        .limit(10)
                    )
                ).all()
            )
            recent_approvals = list(
                (
                    await session.scalars(
                        select(ApprovalRequest)
                        .where(ApprovalRequest.action_type != MEMORY_APPROVAL_ACTION)
                        .order_by(ApprovalRequest.created_at.desc())
                        .limit(10)
                    )
                ).all()
            )
        recent_turns = [
            ChatTurn(
                role=("assistant" if event.direction is EventDirection.OUTBOUND else "user"),
                content=(event.content_text or "")[:4000],
            )
            for event in reversed(recent_events)
            if event.content_text
        ]
        confirmed_memories: list[MemoryContext] = []
        for memory in memories:
            value = memory.value_json.get("value")
            if not value:
                continue
            memory_context = MemoryContext(
                category=memory.category,
                subject=memory.subject,
                predicate=memory.predicate,
                value=str(value),
            )
            if self._is_durable_memory_candidate(memory_context):
                confirmed_memories.append(memory_context)
        active_items = [
            self._timed_context("commitment", item.summary, item.due_at) for item in commitments
        ] + [self._timed_context("task", item.title, item.due_at) for item in tasks]
        recent_notifications = [
            context
            for approval in recent_approvals
            if (context := self._approval_context(approval)) is not None
        ]
        upcoming_calendar = await self._upcoming_calendar_context()
        return ChatRequest(
            message=message,
            recent_turns=recent_turns,
            confirmed_memories=confirmed_memories,
            active_items=active_items,
            recent_notifications=recent_notifications,
            upcoming_calendar=upcoming_calendar,
        )

    async def _upcoming_calendar_context(self) -> list[str]:
        if self._calendar_provider is None:
            return []
        now = require_aware(self._now())
        try:
            events = await self._calendar_provider.list_events(now, now + timedelta(days=7))
        except Exception as exc:
            logger.warning(
                "calendar_context_unavailable",
                extra={"error_type": type(exc).__name__},
            )
            return []
        return [
            self._timed_context("calendar", event.summary, event.start) for event in events[:30]
        ]

    def _timed_context(self, kind: str, summary: str, value: datetime | None) -> str:
        when = (
            value.astimezone(self._timezone).strftime("%d.%m.%Y %H:%M")
            if value is not None
            else "unscheduled"
        )
        return f"{kind}: {summary} @ {when}"

    @staticmethod
    def _approval_context(approval: ApprovalRequest) -> str | None:
        item = approval.action_payload.get("item")
        if not isinstance(item, dict):
            return None
        summary = item.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return None
        parts = [f"alert: {summary.strip()}", f"status: {approval.status.value}"]
        conversation = approval.action_payload.get("conversation_context")
        if isinstance(conversation, dict):
            conversation_type = conversation.get("type")
            display_name = conversation.get("display_name")
            if isinstance(display_name, str) and display_name.strip():
                parts.append(
                    f"source {conversation_type or 'conversation'}: {display_name.strip()}"
                )
        evidence = item.get("evidence")
        if isinstance(evidence, str) and evidence.strip():
            parts.append(f"quoted evidence: {evidence.strip()[:500]}")
        return " | ".join(parts)[:1000]

    @staticmethod
    async def _find_existing_memory(
        session: AsyncSession, candidate: MemoryContext
    ) -> MemoryFact | None:
        matches = list(
            (
                await session.scalars(
                    select(MemoryFact).where(
                        MemoryFact.category == candidate.category,
                        MemoryFact.subject == candidate.subject,
                        MemoryFact.predicate == candidate.predicate,
                        # Rejection is durable feedback too: do not nag with the exact
                        # same memory proposal on a later turn.
                        MemoryFact.status.in_(
                            [
                                MemoryStatus.PROPOSED,
                                MemoryStatus.CONFIRMED,
                                MemoryStatus.REJECTED,
                            ]
                        ),
                    )
                )
            ).all()
        )
        return next(
            (
                memory
                for memory in matches
                if str(memory.value_json.get("value", "")) == candidate.value
            ),
            None,
        )

    @staticmethod
    def _memory_summary(memory: MemoryFact) -> str:
        category_labels = {
            "preference": "העדפה",
            "identity": "פרט אישי",
            "relationship": "קשר",
            "habit": "הרגל",
            "work_style": "סגנון עבודה",
        }
        subject_labels = {"user": "המשתמש", "the user": "המשתמש"}
        predicate_labels = {
            "prefers language": "שפה מועדפת",
            "preferred language": "שפה מועדפת",
            "prefers concise replies": "סגנון תשובות מועדף",
        }
        value_labels = {
            "hebrew": "עברית",
            "yes": "קצר ותמציתי",
            "true": "כן",
            "false": "לא",
        }
        subject = subject_labels.get(memory.subject.casefold(), memory.subject)
        predicate = predicate_labels.get(memory.predicate.casefold(), memory.predicate)
        raw_value = str(memory.value_json.get("value", ""))
        value = value_labels.get(raw_value.casefold(), raw_value)
        category = category_labels.get(memory.category.casefold(), memory.category)
        return f"👤 {subject}\n💡 {predicate}: {value}\n🏷️ קטגוריה: {category}"

    @staticmethod
    def _is_durable_memory_candidate(candidate: MemoryContext) -> bool:
        category = candidate.category.casefold().strip()
        predicate = candidate.predicate.casefold().strip()
        transient_categories = {
            "commitment",
            "task",
            "todo",
            "plan",
            "schedule",
            "appointment",
            "reminder",
        }
        transient_predicates = {
            "has commitment",
            "has task",
            "needs to",
            "plans to",
            "will do",
        }
        return category not in transient_categories and predicate not in transient_predicates
