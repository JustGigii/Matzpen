import uuid
from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.core.time import require_aware
from personal_agent.domain.enums import ActionClass, ApprovalStatus
from personal_agent.domain.models import ApprovalRequest, AuditLog
from personal_agent.domain.schemas import CommitmentExtraction, ExtractionResult
from personal_agent.services.lifecycle import (
    CLARIFICATION_ACTION,
    EXTRACTION_CONFIRMATION_ACTION,
    LifecycleService,
)


class ConfirmationService:
    """Persists and resolves user confirmation for an ambiguous extraction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        now: Callable[[], datetime],
        lifecycle: LifecycleService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._now = now
        self._lifecycle = lifecycle

    async def create(self, result: ExtractionResult) -> uuid.UUID:
        approval_id = uuid.uuid4()
        async with self._session_factory() as session:
            session.add(
                ApprovalRequest(
                    id=approval_id,
                    action_type=EXTRACTION_CONFIRMATION_ACTION,
                    action_payload=result.model_dump(mode="json"),
                    risk_class=ActionClass.OBSERVE.value,
                    status=ApprovalStatus.PENDING,
                )
            )
            session.add(
                AuditLog(
                    actor="direct_message_service",
                    action="request_extraction_confirmation",
                    target=str(approval_id),
                    policy_decision="user_confirmation_required",
                    approval_id=approval_id,
                    result="pending",
                )
            )
            await session.commit()
        return approval_id

    async def resolve(self, approval_id: uuid.UUID, *, approve: bool) -> bool:
        if approve and self._lifecycle is not None:
            async with self._session_factory() as session:
                approval = await session.get(ApprovalRequest, approval_id)
                if (
                    approval is not None
                    and approval.status is ApprovalStatus.PENDING
                    and approval.action_type
                    in {EXTRACTION_CONFIRMATION_ACTION, CLARIFICATION_ACTION}
                    and "item" in approval.action_payload
                ):
                    return await self._lifecycle.execute_approval(
                        approval_id, resolution_source="user_confirmed"
                    )
        async with self._session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if (
                approval is None
                or approval.action_type != EXTRACTION_CONFIRMATION_ACTION
                or approval.status is not ApprovalStatus.PENDING
            ):
                return False
            approval.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
            approval.resolved_at = require_aware(self._now())
            session.add(
                AuditLog(
                    actor="telegram_user",
                    action="resolve_extraction_confirmation",
                    target=str(approval_id),
                    policy_decision="explicit_user_decision",
                    approval_id=approval_id,
                    result="approved" if approve else "rejected",
                )
            )
            await session.commit()
            return True

    async def resolve_time(self, approval_id: uuid.UUID, due_at: datetime) -> bool:
        if self._lifecycle is None:
            return False
        return await self._lifecycle.execute_approval(
            approval_id,
            resolution_source="user_selected_time",
            due_at=require_aware(due_at),
        )

    async def resolve_suggested_hour(
        self, approval_id: uuid.UUID, hour: int, timezone: str
    ) -> bool:
        if not 0 <= hour <= 23:
            return False
        async with self._session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if approval is None or approval.status is not ApprovalStatus.PENDING:
                return False
            item = CommitmentExtraction.model_validate(approval.action_payload["item"])
        zone = ZoneInfo(timezone)
        local_now = require_aware(self._now()).astimezone(zone)
        selected = (
            datetime.combine(item.explicit_date, time(hour), zone)
            if item.explicit_date is not None
            else local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
        )
        if item.explicit_date is None and selected <= local_now:
            selected += timedelta(days=1)
        return await self.resolve_time(approval_id, selected)

    async def toggle_timetable_row(self, approval_id: uuid.UUID, index: int) -> bool:
        async with self._session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if approval is None or approval.status is not ApprovalStatus.PENDING:
                return False
            item = CommitmentExtraction.model_validate(approval.action_payload["item"])
            if not 0 <= index < len(item.timetable_rows):
                return False
            rows = list(item.timetable_rows)
            rows[index] = rows[index].model_copy(update={"included": not rows[index].included})
            updated = item.model_copy(update={"timetable_rows": rows})
            approval.action_payload = {
                **approval.action_payload,
                "item": updated.model_dump(mode="json"),
            }
            session.add(
                AuditLog(
                    actor="telegram_user",
                    action="toggle_timetable_row",
                    target=str(approval_id),
                    approval_id=approval_id,
                    result="included" if rows[index].included else "excluded",
                    redacted_metadata={"row_index": index},
                )
            )
            await session.commit()
            return True
