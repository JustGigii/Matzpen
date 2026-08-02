import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.domain.enums import (
    ActionClass,
    ApprovalStatus,
    CommitmentStatus,
    ProcessingStatus,
    TaskStatus,
)
from personal_agent.domain.models import ApprovalRequest, AuditLog, Commitment, Event, Task
from personal_agent.domain.schemas import (
    CommitmentExtraction,
    ExtractionRequest,
    IntakeResult,
    NormalizedEvent,
)
from personal_agent.integrations.llm.base import LLMProvider
from personal_agent.integrations.telegram.base import TelegramNotifier
from personal_agent.integrations.telegram.presentation import localized_summary
from personal_agent.repositories.events import EventRepository
from personal_agent.services.lifecycle import (
    CLARIFICATION_ACTION,
    COMMITMENT_WORKFLOW_ACTION,
    EXTRACTION_CONFIRMATION_ACTION,
    LifecycleService,
)
from personal_agent.services.policy import ApprovalPolicy

REMINDER_ACTION_TYPE = COMMITMENT_WORKFLOW_ACTION
HIGH_CONFIDENCE_THRESHOLD = 0.90


def calculate_dedupe_key(event: NormalizedEvent) -> str:
    material = {
        "source": event.source.value,
        "source_account": event.source_account,
        "external_id": event.external_id,
        "event_type": event.event_type,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


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

    async def ingest(self, candidate: NormalizedEvent) -> IntakeResult:
        if candidate.dedupe_key is None:
            candidate = candidate.model_copy(update={"dedupe_key": calculate_dedupe_key(candidate)})

        async with self._session_factory() as session:
            repository = EventRepository(session)
            event, created = await repository.add_if_absent(candidate)
            await session.commit()
            if not created:
                return IntakeResult(event_id=str(event.id), created=False)

            try:
                extraction = await self._llm_provider.extract_event(
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
                commitment_ids: list[str] = []
                task_ids: list[str] = []
                approvals: list[ApprovalRequest] = []
                force_confirmation = bool(event.payload_json.get("force_confirmation", False))
                for index, item in enumerate(extraction.items):
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
                await session.commit()

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
                        if approval.action_type == CLARIFICATION_ACTION:
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
            except Exception:
                await session.rollback()
                stored_event = await session.get(type(event), event.id)
                if stored_event is not None:
                    stored_event.processing_status = ProcessingStatus.FAILED
                    await session.commit()
                raise

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
        ):
            return False
        try:
            LifecycleService.validate_item(item, self._now(), allow_untimed=True)
        except ValueError:
            return False
        return True

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
        needs_time = item.due_at is None and (
            item.ambiguous or item.needs_clarification or item.requires_user_confirmation
        )
        action_type = (
            COMMITMENT_WORKFLOW_ACTION
            if automatic
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
    def _preview_text(
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
        calendar_event = item_payload.get("calendar_event")
        if isinstance(calendar_event, dict):
            lines.append(f"🗓️ ביומן: {calendar_event.get('start')} — {calendar_event.get('end')}")
        rows = item_payload.get("timetable_rows")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("included", True):
                    lines.append(f"• {row.get('summary')} · {row.get('start')} — {row.get('end')}")
        if isinstance(conversation_context, dict):
            display_name = conversation_context.get("display_name")
            if conversation_context.get("type") == "private":
                context_label = (
                    f"💬 שיחת WhatsApp עם {display_name}"
                    if isinstance(display_name, str) and display_name.strip()
                    else "💬 שיחת WhatsApp פרטית — השם לא התקבל"
                )
                lines.append(context_label)
        return "\n".join(lines)
