import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.domain.enums import (
    ActionClass,
    ActionType,
    ApprovalStatus,
    CommitmentStatus,
    ProcessingStatus,
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
from personal_agent.domain.schemas import (
    CalendarProposal,
    CommitmentExtraction,
    ExtractionPerson,
    ExtractionRequest,
    ExtractionResult,
    IntakeResult,
    NormalizedEvent,
)
from personal_agent.integrations.llm.base import (
    LLMProvider,
    LLMQuotaExceededError,
    LLMRetryableError,
)
from personal_agent.integrations.telegram.base import TelegramNotifier
from personal_agent.integrations.telegram.presentation import localized_summary
from personal_agent.repositories.events import EventRepository
from personal_agent.services.lifecycle import (
    CLARIFICATION_ACTION,
    COMMITMENT_WORKFLOW_ACTION,
    DETAIL_CLARIFICATION_ACTION,
    EXTRACTION_CONFIRMATION_ACTION,
    LifecycleService,
)
from personal_agent.services.policy import ApprovalPolicy

REMINDER_ACTION_TYPE = COMMITMENT_WORKFLOW_ACTION
HIGH_CONFIDENCE_THRESHOLD = 0.90
LINK_TOKEN_PATTERN = re.compile(
    r"(?:קיש[ון]?ר|לינק|\blink\b)",  # noqa: RUF001
    re.IGNORECASE,
)
LINK_PROMISE_PATTERN = re.compile(
    r"(?:אשלח|אני\s+אשלח|i(?:['’]ll|\s+will)\s+send)",  # noqa: RUF001
    re.IGNORECASE,
)
CONTEXT_FOLLOWUP_PATTERN = re.compile(
    r"^\s*(?:(?:מאיז(?:ה|ו)|איזו?|מה|על\s+מה|מי|איפה)\b.{0,80}"  # noqa: RUF001
    r"(?:קבוצה|הקשר|מדובר|כתב|נכתב|נשלח)|"
    r"(?:תן|תני)\s+(?:לי\s+)?(?:עוד|יותר)\s+פרטים)",
    re.IGNORECASE,
)
CONVERSATIONAL_FOLLOWUP_PATTERN = re.compile(
    r"^\s*(?:(?:תוכל|תוכלי|אפשר|אתה\s+יכול|את\s+יכולה)\s+"
    r"(?:לכתוב|לנסח|לשכתב)\s+(?:את\s+)?(?:זה|זאת)\b|"
    r"(?:כתוב|נסח|שכתב)\s+(?:את\s+)?(?:זה|זאת)\b|"
    r"(?:please\s+)?(?:rewrite|rephrase)\s+(?:this|that)\b)",
    re.IGNORECASE,
)
NON_ACTION_CONTROL_PATTERN = re.compile(
    r"^\s*(?:(?:מה|איזה|אילו|הצג|תראה|בדוק)\b.{0,40}"
    r"(?:משימ\w*|התחייב\w*|פתוח).{0,30}|"
    r"(?:משימ\w*\s*\d*\s*)?(?:בוצע|בוצעה|סיימתי|בטל|ביטול|מחק)"
    r"(?:\s+משימ\w*\s*\d*)?)\s*[?!.]*\s*$",
    re.IGNORECASE,
)
GENERIC_CLARIFICATION_OPTION_PATTERN = re.compile(
    r"^(?:(?:ה)?(?:נושא|אפשרות|בחירה)\s*(?:מספר\s*)?(?:ה)?"
    r"(?:ראשון|ראשונה|שני|שנייה|שניה|שלישי|שלישית|רביעי|רביעית|[1-4])|"
    r"(?:the\s+)?(?:first|second|third|fourth)\s+(?:topic|option|choice)|"
    r"(?:topic|option|choice)\s+(?:one|two|three|four|[1-4]))$",
    re.IGNORECASE,
)


def calculate_dedupe_key(event: NormalizedEvent) -> str:
    material = {
        "source": event.source.value,
        "source_account": event.source_account,
        "external_id": event.external_id,
        "event_type": event.event_type,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ClarificationResolution:
    state: Literal["executed", "question", "time", "confirmation", "inactive"]
    summary: str = ""
    question: str | None = None
    options: tuple[str, ...] = ()


class IntakeService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        llm_provider: LLMProvider,
        notifier: TelegramNotifier,
        policy: ApprovalPolicy,
        lifecycle: LifecycleService,
        now: Callable[[], datetime],
        reminder_lead_minutes: int,
        clarification_fallback_minutes: int = 10,
        approval_expiry_hours: int = 24,
        timezone: str = "Asia/Jerusalem",
    ) -> None:
        self._session_factory = session_factory
        self._llm_provider = llm_provider
        self._notifier = notifier
        self._policy = policy
        self._lifecycle = lifecycle
        self._now = now
        self._reminder_lead_minutes = reminder_lead_minutes
        self._clarification_fallback_minutes = clarification_fallback_minutes
        self._approval_expiry_hours = approval_expiry_hours
        self._timezone = ZoneInfo(timezone)

    async def ingest(self, candidate: NormalizedEvent) -> IntakeResult:
        if candidate.dedupe_key is None:
            candidate = candidate.model_copy(update={"dedupe_key": calculate_dedupe_key(candidate)})

        async with self._session_factory() as session:
            repository = EventRepository(session)
            event, created = await repository.add_if_absent(candidate)
            await session.commit()
            retrying_quota_failure = (
                not created
                and event.processing_status is ProcessingStatus.FAILED
                and event.payload_json.get("retryable_failure") in {"llm_quota", "llm_unavailable"}
            )
            if not created and not retrying_quota_failure:
                return IntakeResult(event_id=str(event.id), created=False)

            try:
                is_conversational = self._is_context_followup(event)
                extraction = (
                    ExtractionResult(
                        language=(
                            "he"
                            if re.search(r"[\u0590-\u05ff]", event.content_text or "")
                            else "en"
                        ),
                        items=[],
                    )
                    if is_conversational
                    else await self._llm_provider.extract_event(
                        ExtractionRequest(
                            event_id=str(event.id),
                            event_type=event.event_type,
                            direction=event.direction,
                            occurred_at=event.occurred_at,
                            content_text=event.content_text or "",
                            conversation_display_name=self._conversation_display_name(event),
                            conversation_type=self._conversation_type(event),
                        )
                    )
                )
                commitment_ids: list[str] = []
                task_ids: list[str] = []
                approvals: list[ApprovalRequest] = []
                superseded_message_ids: list[str] = []
                force_confirmation = bool(event.payload_json.get("force_confirmation", False))
                items = self._ensure_explicit_link_commitment(extraction.items, event)
                for index, item in enumerate(items):
                    item = self._enrich_item(item, event)
                    item_dedupe_key = f"event:{event.id}:item:{index}"
                    automatic = self._can_use_grace_period(
                        item,
                        force_confirmation=force_confirmation,
                    )
                    commitment_id: str | None = None
                    task_id: str | None = None
                    commitment: Commitment | None = None
                    task: Task | None = None
                    if automatic and item.kind == "commitment":
                        commitment = Commitment(
                            direction=item.direction,
                            action_type=item.action_type,
                            summary=item.summary,
                            due_at=item.due_at,
                            source_event_id=event.id,
                            status=CommitmentStatus.DETECTED,
                            confidence=item.confidence,
                            reminder_lead_minutes=self._reminder_lead_minutes,
                            dedupe_key=item_dedupe_key,
                        )
                        session.add(commitment)
                        await session.flush()
                        commitment_id = str(commitment.id)
                        commitment_ids.append(commitment_id)
                    elif automatic and item.kind == "task":
                        superseded_message_ids.extend(
                            await self._supersede_similar_task(session, event, item)
                        )
                        task = Task(
                            title=item.summary,
                            description=item.evidence,
                            status=TaskStatus.PENDING,
                            due_at=item.due_at,
                            source_event_id=event.id,
                            confidence=item.confidence,
                            dedupe_key=item_dedupe_key,
                        )
                        session.add(task)
                        await session.flush()
                        task_id = str(task.id)
                        task_ids.append(task_id)

                    approval = self._create_approval(
                        event.id,
                        item,
                        extraction.language,
                        event.content_text or "",
                        item_dedupe_key,
                        commitment_id,
                        task_id,
                        automatic,
                        self._conversation_context(event),
                    )
                    session.add(approval)
                    await session.flush()
                    if commitment is not None:
                        commitment.source_approval_id = approval.id
                        await self._lifecycle.stage_calendar_actions(session, commitment, item)
                    elif task is not None:
                        await self._lifecycle.stage_task_calendar_actions(
                            session, task, item, approval.id
                        )
                    approvals.append(approval)
                    session.add(
                        AuditLog(
                            actor="intake_service",
                            action=(
                                "propose_internal_workflow"
                                if automatic
                                else "request_extraction_resolution"
                            ),
                            target=commitment_id or task_id or item_dedupe_key,
                            policy_decision=ActionClass.INTERNAL_REVERSIBLE.value,
                            source_event_id=event.id,
                            approval_id=approval.id,
                            result=("pending_grace_period" if automatic else "pending_user_input"),
                            redacted_metadata={"confidence": item.confidence},
                        )
                    )

                await repository.set_processing_status(event, ProcessingStatus.PROCESSED)
                if retrying_quota_failure:
                    event.payload_json = {
                        key: value
                        for key, value in event.payload_json.items()
                        if key != "retryable_failure"
                    }
                await session.commit()

                for message_id in dict.fromkeys(superseded_message_ids):
                    await self._notifier.workflow_confirmation(
                        message_id,
                        "🔁 המשימה הוחלפה בניסוח המעודכן. הכרטיס הישן אינו פעיל.",
                    )

                for approval in approvals:
                    if approval.action_type == COMMITMENT_WORKFLOW_ACTION:
                        message_id = await self._notifier.pending_internal_action(
                            str(approval.id),
                            self._preview_text(
                                approval.action_payload["item"],
                                approval.action_payload.get("conversation_context"),
                            ),
                            approval.execute_after or self._now(),
                        )
                    else:
                        item_payload = approval.action_payload["item"]
                        detail = self._preview_text(
                            item_payload,
                            approval.action_payload.get("conversation_context"),
                        )
                        if approval.action_type == DETAIL_CLARIFICATION_ACTION:
                            question, options = self._detail_prompt(item_payload)
                            message_id = await self._notifier.detail_clarification_request(
                                str(approval.id), detail, question, options
                            )
                        elif approval.action_type == CLARIFICATION_ACTION:
                            detail += "\nהשעה אינה ברורה. אפשר לבחור שעה או לבטל."
                            message_id = await self._notifier.clarification_request(
                                str(approval.id), detail, (18, 19, 20)
                            )
                        elif item_payload.get("timetable_rows"):
                            rows = item_payload["timetable_rows"]
                            message_id = await self._notifier.timetable_request(
                                str(approval.id), detail, len(rows)
                            )
                        else:
                            message_id = await self._notifier.approval_request(
                                str(approval.id), detail
                            )
                    approval.telegram_message_id = message_id
                await session.commit()
                return IntakeResult(
                    event_id=str(event.id),
                    created=True,
                    commitment_ids=commitment_ids,
                    task_ids=task_ids,
                    approval_ids=[str(approval.id) for approval in approvals],
                )
            except LLMRetryableError as exc:
                await session.rollback()
                stored_event = await session.get(type(event), event.id)
                if stored_event is not None:
                    stored_event.processing_status = ProcessingStatus.FAILED
                    stored_event.payload_json = {
                        **stored_event.payload_json,
                        "retryable_failure": (
                            "llm_quota"
                            if isinstance(exc, LLMQuotaExceededError)
                            else "llm_unavailable"
                        ),
                    }
                    await session.commit()
                raise
            except Exception:
                await session.rollback()
                stored_event = await session.get(type(event), event.id)
                if stored_event is not None:
                    stored_event.processing_status = ProcessingStatus.FAILED
                    await session.commit()
                raise

    async def refine_approval(self, approval_id: uuid.UUID, answer: str) -> ClarificationResolution:
        """Apply a user's answer to the same pending extraction proposal.

        The LLM interprets the answer in the bounded context of the original message and question.
        Nothing is materialized until the ambiguity is resolved.
        """
        clean_answer = " ".join(answer.strip().split())[:1000]
        if not clean_answer:
            return ClarificationResolution(state="inactive")

        async with self._session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if (
                approval is None
                or approval.status is not ApprovalStatus.PENDING
                or approval.action_type != DETAIL_CLARIFICATION_ACTION
                or approval.source_event_id is None
            ):
                return ClarificationResolution(state="inactive")
            event = await session.get(Event, approval.source_event_id)
            if event is None:
                return ClarificationResolution(state="inactive")
            current_item = CommitmentExtraction.model_validate(approval.action_payload["item"])
            history = approval.action_payload.get("clarification_history", [])
            history_lines = [
                f"- {entry.get('question', '')} => {entry.get('answer', '')}"
                for entry in history
                if isinstance(entry, dict)
            ]
            current_question, _ = self._detail_prompt(approval.action_payload["item"])
            request = ExtractionRequest(
                event_id=str(event.id),
                event_type=f"{event.event_type}.clarification",
                direction=event.direction,
                occurred_at=event.occurred_at,
                content_text=(
                    f"Original message:\n{event.content_text or ''}\n\n"
                    f"Current extracted proposal:\n{current_item.summary}\n\n"
                    f"Previous clarifications:\n{chr(10).join(history_lines) or '(none)'}\n\n"
                    f"Assistant question:\n{current_question}\n\n"
                    f"User answer:\n{clean_answer}\n\n"
                    "Interpret the user answer only as clarification of the original proposal. "
                    "Return exactly that clarified item, or ask the next necessary clarification."
                ),
                conversation_display_name=self._conversation_display_name(event),
                conversation_type=self._conversation_type(event),
            )

        extraction = await self._llm_provider.extract_event(request)
        if len(extraction.items) != 1:
            return ClarificationResolution(
                state="question",
                summary=current_item.summary,
                question="לא הצלחתי להבין איזה פרט להשלים. אפשר לנסח אותו במילים שלך?",
            )
        item = self._enrich_item(extraction.items[0], event)
        async with self._session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if (
                approval is None
                or approval.status is not ApprovalStatus.PENDING
                or approval.action_type != DETAIL_CLARIFICATION_ACTION
            ):
                return ClarificationResolution(state="inactive")
            payload = dict(approval.action_payload)
            prior_history = payload.get("clarification_history", [])
            payload["clarification_history"] = [
                *(prior_history if isinstance(prior_history, list) else []),
                {"question": current_question, "answer": clean_answer},
            ]
            payload["item"] = item.model_dump(mode="json")
            approval.action_payload = payload
            question, options = self._detail_prompt(payload["item"])
            if item.clarification_question:
                await session.commit()
                return ClarificationResolution(
                    state="question",
                    summary=item.summary,
                    question=question,
                    options=options,
                )
            if item.due_at is None and (
                item.ambiguous or item.needs_clarification or item.requires_user_confirmation
            ):
                approval.action_type = CLARIFICATION_ACTION
                approval.execute_after = self._now() + timedelta(
                    minutes=self._clarification_fallback_minutes
                )
                await session.commit()
                return ClarificationResolution(state="time", summary=item.summary)
            requires_confirmation = bool(
                item.calendar_worthy or item.calendar_event is not None or item.timetable_rows
            )
            approval.action_type = EXTRACTION_CONFIRMATION_ACTION
            approval.execute_after = None
            await session.commit()

        if requires_confirmation:
            return ClarificationResolution(state="confirmation", summary=item.summary)
        executed = await self._lifecycle.execute_approval(
            approval_id, resolution_source="user_clarified"
        )
        return ClarificationResolution(
            state="executed" if executed else "inactive", summary=item.summary
        )

    @staticmethod
    def _conversation_display_name(event: Event) -> str | None:
        value = event.payload_json.get("conversation_display_name")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _conversation_type(event: Event) -> str | None:
        value = event.payload_json.get("conversation_type")
        return value if isinstance(value, str) and value in {"private", "group"} else None

    @classmethod
    def _conversation_context(cls, event: Event) -> dict[str, str | None] | None:
        conversation_type = cls._conversation_type(event)
        if conversation_type is None:
            return None
        return {
            "display_name": cls._conversation_display_name(event),
            "type": conversation_type,
        }

    def _can_use_grace_period(
        self,
        item: CommitmentExtraction,
        *,
        force_confirmation: bool = False,
    ) -> bool:
        if (
            force_confirmation
            or item.confidence < HIGH_CONFIDENCE_THRESHOLD
            or item.requires_user_confirmation
            or item.ambiguous
            or item.needs_clarification
            or item.calendar_worthy
            or item.calendar_event is not None
            or bool(item.timetable_rows)
        ):
            return False
        try:
            LifecycleService.validate_item(item, self._now(), allow_untimed=True)
        except ValueError:
            return False
        return True

    def _enrich_item(self, item: CommitmentExtraction, event: Event) -> CommitmentExtraction:
        """Apply deterministic meeting/calendar defaults after validating LLM output."""
        if item.action_type is not ActionType.MEET or item.due_at is None:
            return item
        conversation_name = self._conversation_display_name(event)
        source_label = (
            f"שיחת WhatsApp עם {conversation_name}"
            if conversation_name
            else "שיחה שנקלטה בסוכן האישי"
        )
        description_lines = [source_label, f"התחייבות: {item.summary}", f"מקור: {item.evidence}"]
        if item.calendar_event is None:
            proposal = CalendarProposal(
                summary=item.summary,
                start=item.due_at,
                end=item.due_at + timedelta(hours=1),
                description="\n".join(description_lines),
            )
        else:
            existing_description = item.calendar_event.description
            proposal = item.calendar_event.model_copy(
                update={
                    "description": existing_description or "\n".join(description_lines),
                }
            )
        return item.model_copy(
            update={
                "calendar_worthy": True,
                "calendar_event": proposal,
            }
        )

    async def _supersede_similar_task(
        self,
        session: AsyncSession,
        event: Event,
        item: CommitmentExtraction,
    ) -> list[str]:
        """Deactivate a recent same-conversation task when the user refines its wording."""
        cutoff = event.occurred_at - timedelta(minutes=10)
        rows = list(
            (
                await session.execute(
                    select(Task, Event)
                    .join(Event, Task.source_event_id == Event.id)
                    .where(
                        Task.status == TaskStatus.PENDING,
                        Event.source == event.source,
                        Event.conversation_external_id == event.conversation_external_id,
                        Event.occurred_at >= cutoff,
                        Event.occurred_at <= event.occurred_at,
                        Event.id != event.id,
                    )
                )
            ).all()
        )
        normalized_new = self._normalize_duplicate_text(item.summary)
        match: tuple[Task, Event] | None = None
        best_score = 0.0
        for candidate_task, candidate_event in rows:
            score = SequenceMatcher(
                None,
                self._normalize_duplicate_text(candidate_task.title),
                normalized_new,
            ).ratio()
            if score >= 0.78 and score > best_score:
                match = (candidate_task, candidate_event)
                best_score = score
        if match is None:
            return []

        old_task, old_event = match
        old_task.status = TaskStatus.CANCELLED
        reminders = list(
            (await session.scalars(select(Reminder).where(Reminder.task_id == old_task.id))).all()
        )
        message_ids = [
            reminder.telegram_message_id
            for reminder in reminders
            if reminder.telegram_message_id is not None
        ]
        for reminder in reminders:
            if reminder.status in {ReminderStatus.PENDING, ReminderStatus.SENT}:
                reminder.status = ReminderStatus.HANDLED

        pending_approvals = list(
            (
                await session.scalars(
                    select(ApprovalRequest).where(
                        ApprovalRequest.status == ApprovalStatus.PENDING,
                        ApprovalRequest.source_event_id == old_event.id,
                    )
                )
            ).all()
        )
        for approval in pending_approvals:
            if approval.action_payload.get("task_id") != str(old_task.id):
                continue
            approval.status = ApprovalStatus.REJECTED
            approval.resolved_at = self._now()
            if approval.telegram_message_id is not None:
                message_ids.append(approval.telegram_message_id)
        session.add(
            AuditLog(
                actor="intake_service",
                action="supersede_duplicate_task",
                target=str(old_task.id),
                policy_decision=ActionClass.INTERNAL_REVERSIBLE.value,
                source_event_id=event.id,
                result="replaced",
                redacted_metadata={"similarity": round(best_score, 3)},
            )
        )
        return message_ids

    @staticmethod
    def _normalize_duplicate_text(value: str) -> str:
        return " ".join(re.sub(r"[^\w\s]", " ", value.casefold()).split())

    @staticmethod
    def _is_context_followup(event: Event) -> bool:
        if not event.content_text:
            return False
        return (
            CONTEXT_FOLLOWUP_PATTERN.search(event.content_text) is not None
            or CONVERSATIONAL_FOLLOWUP_PATTERN.search(event.content_text) is not None
            or IntakeService._looks_like_rewrite_followup(event.content_text)
            or NON_ACTION_CONTROL_PATTERN.search(event.content_text) is not None
        )

    @staticmethod
    def _looks_like_rewrite_followup(value: str) -> bool:
        normalized = " ".join(value.casefold().split())
        if not re.search(r"\bאת\s+(?:זה|זאת)\b", normalized):
            return False
        return any(
            max(
                SequenceMatcher(None, token, candidate).ratio()
                for candidate in ("לכתוב", "לנסח", "לשכתב")
            )
            >= 0.67
            for token in normalized.split()
        )

    def _ensure_explicit_link_commitment(
        self,
        items: list[CommitmentExtraction],
        event: Event,
    ) -> list[CommitmentExtraction]:
        """Keep an explicit send-link promise separate from its meeting.

        Models sometimes collapse "I will send a link" into the meeting itself even though it is
        independently remindable. The deterministic supplement only runs when the source contains
        a first-person send promise and the model already found a timed meeting to anchor it to.
        """
        source_text = event.content_text or ""
        evidence = next(
            (
                line.strip()
                for line in source_text.splitlines()
                if LINK_PROMISE_PATTERN.search(line) and LINK_TOKEN_PATTERN.search(line)
            ),
            None,
        )
        if evidence is None or any(
            item.action_type in {ActionType.SEND, ActionType.MESSAGE}
            and LINK_TOKEN_PATTERN.search(f"{item.summary}\n{item.evidence}")
            for item in items
        ):
            return items
        meeting = next(
            (
                item
                for item in items
                if item.action_type is ActionType.MEET and item.due_at is not None
            ),
            None,
        )
        if meeting is None:
            return items
        conversation_name = self._conversation_display_name(event)
        person = (
            ExtractionPerson(display_name=conversation_name)
            if conversation_name
            else meeting.person
        )
        summary = "לשלוח קישור לפגישת Zoom"
        if conversation_name:
            summary += f" עם {conversation_name}"
        link_item = CommitmentExtraction(
            kind="commitment",
            summary=summary,
            action_type=ActionType.SEND,
            direction=meeting.direction,
            due_at=meeting.due_at,
            explicit_date=meeting.explicit_date,
            person=person,
            confidence=min(meeting.confidence, 0.95),
            evidence=evidence,
            requires_user_confirmation=meeting.requires_user_confirmation,
        )
        return [*items, link_item]

    def _create_approval(
        self,
        source_event_id: uuid.UUID,
        item: CommitmentExtraction,
        language: str,
        source_text: str,
        item_dedupe_key: str,
        commitment_id: str | None,
        task_id: str | None,
        automatic: bool,
        conversation_context: dict[str, str | None] | None,
    ) -> ApprovalRequest:
        decision = self._policy.decide(ActionClass.INTERNAL_REVERSIBLE)
        now = self._now()
        expiry = now + timedelta(hours=self._approval_expiry_hours)
        if item.due_at is not None and now < item.due_at < expiry:
            expiry = item.due_at
        needs_details = bool(item.clarification_question)
        needs_time = (
            not needs_details
            and item.due_at is None
            and (item.ambiguous or item.needs_clarification or item.requires_user_confirmation)
        )
        action_type = (
            COMMITMENT_WORKFLOW_ACTION
            if automatic
            else DETAIL_CLARIFICATION_ACTION
            if needs_details
            else CLARIFICATION_ACTION
            if needs_time
            else EXTRACTION_CONFIRMATION_ACTION
        )
        execute_after = None
        if automatic:
            if decision.grace_seconds is None:
                raise RuntimeError("Internal workflow policy must provide a grace period")
            execute_after = now + timedelta(seconds=decision.grace_seconds)
        elif needs_time:
            execute_after = now + timedelta(minutes=self._clarification_fallback_minutes)
        return ApprovalRequest(
            action_type=action_type,
            action_payload={
                "item": item.model_dump(mode="json"),
                "language": language,
                "source_text": source_text,
                "item_dedupe_key": item_dedupe_key,
                "commitment_id": commitment_id,
                "task_id": task_id,
                "conversation_context": conversation_context,
            },
            risk_class=ActionClass.INTERNAL_REVERSIBLE.value,
            status=ApprovalStatus.PENDING,
            execute_after=execute_after,
            expires_at=expiry,
            source_event_id=source_event_id,
            dedupe_key=f"{item_dedupe_key}:approval",
        )

    @staticmethod
    def _detail_prompt(item_payload: dict[str, object]) -> tuple[str, tuple[str, ...]]:
        raw_question = item_payload.get("clarification_question")
        question = (
            str(raw_question).strip()
            if isinstance(raw_question, str) and raw_question.strip()
            else "איזה פרט חסר כדי שאוכל לשמור את ההתחייבות נכון?"
        )
        raw_options = item_payload.get("clarification_options")
        options = tuple(
            str(option).strip()[:64]
            for option in (raw_options if isinstance(raw_options, list) else [])[:4]
            if isinstance(option, str)
            and option.strip()
            and GENERIC_CLARIFICATION_OPTION_PATTERN.fullmatch(option.strip()) is None
        )
        return question, options

    def _preview_text(
        self,
        item_payload: dict[str, object],
        conversation_context: object = None,
    ) -> str:
        person_payload = item_payload.get("person")
        person_name = (
            str(person_payload.get("display_name"))
            if isinstance(person_payload, dict) and person_payload.get("display_name")
            else None
        )
        lines = [
            localized_summary(
                str(item_payload["summary"]),
                str(item_payload["action_type"]),
                person_name,
            )
        ]
        if isinstance(conversation_context, dict):
            display_name = conversation_context.get("display_name")
            if conversation_context.get("type") == "private":
                context_label = (
                    f"📍 מקור: שיחת WhatsApp עם {display_name}"
                    if isinstance(display_name, str) and display_name.strip()
                    else "📍 מקור: שיחת WhatsApp פרטית — השם לא התקבל"
                )
                lines.append(context_label)
            elif conversation_context.get("type") == "group":
                context_label = (
                    f"📍 מקור: קבוצת WhatsApp — {display_name}"
                    if isinstance(display_name, str) and display_name.strip()
                    else "📍 מקור: קבוצת WhatsApp — השם לא התקבל"
                )
                lines.append(context_label)
        due_at = self._payload_datetime(item_payload.get("due_at"))
        if due_at is not None:
            lines.append(f"📅 מועד: {due_at.astimezone(self._timezone):%d.%m.%Y בשעה %H:%M}")
            proposed_leads = item_payload.get("reminder_lead_minutes")
            leads = (
                [int(lead) for lead in proposed_leads if isinstance(lead, int)]
                if isinstance(proposed_leads, list) and proposed_leads
                else [30, 10]
                if bool(item_payload.get("calendar_worthy"))
                else [self._reminder_lead_minutes, 0]
            )
            reminder_labels = [
                "במועד" if lead == 0 else f"{lead} דקות לפני" for lead in sorted(set(leads))
            ]
            lines.append(f"🔔 התראות: {', '.join(reminder_labels)}")
        calendar_event = item_payload.get("calendar_event")
        if isinstance(calendar_event, dict):
            start = self._payload_datetime(calendar_event.get("start"))
            end = self._payload_datetime(calendar_event.get("end"))
            if start is not None and end is not None:
                lines.append(
                    "🗓️ יתווסף ל־Google Calendar: "
                    f"{start.astimezone(self._timezone):%d.%m %H:%M}-"
                    f"{end.astimezone(self._timezone):%H:%M}"
                )
        rows = item_payload.get("timetable_rows")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("included", True):
                    lines.append(f"• {row.get('summary')} · {row.get('start')} — {row.get('end')}")
        evidence = item_payload.get("evidence")
        if isinstance(evidence, str) and evidence.strip():
            lines.append(f"🔎 מהשיחה: {evidence.strip()}")
        return "\n".join(lines)

    @staticmethod
    def _payload_datetime(value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
