import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from typing import cast

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.domain.enums import ApprovalStatus, TaskStatus
from personal_agent.domain.models import ApprovalRequest, Task
from personal_agent.domain.schemas import CommitmentExtraction, ExtractionResult
from personal_agent.integrations.llm.fake import FakeLLMProvider
from personal_agent.integrations.telegram.fake import FakeTelegramNotifier
from personal_agent.services.intake import IntakeService
from personal_agent.services.lifecycle import DETAIL_CLARIFICATION_ACTION


def test_detail_prompt_keeps_only_concrete_self_explanatory_options() -> None:
    question, options = IntakeService._detail_prompt(
        {
            "clarification_question": "על איזה נושא תרצה לדבר?",
            "clarification_options": [
                "הנושא הראשון",
                "אפשרות 2",
                "תכנון הטיול המשפחתי",
                "the third option",
            ],
        }
    )

    assert question == "על איזה נושא תרצה לדבר?"
    assert options == ("תכנון הטיול המשפחתי",)


async def _post_shortcut(client: AsyncClient, content: str, captured_at: datetime) -> dict:
    response = await client.post(
        "/api/intake/shortcut",
        headers={"Authorization": "Bearer test-shortcut-token"},
        json={
            "type": "text",
            "content": content,
            "captured_at": captured_at.isoformat(),
            "metadata": {"source": "test"},
        },
    )
    assert response.status_code == 200
    return response.json()


async def test_missing_detail_is_asked_then_same_approval_materializes_task(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    initial = CommitmentExtraction(
        kind="task",
        summary="לקבוע תור",
        action_type="other",
        confidence=0.83,
        evidence="לקבוע תור",
        ambiguous=True,
        needs_clarification=True,
        clarification_question="לאיזה שירות לקבוע תור?",
        clarification_options=["רופא משפחה", "רופא שיניים"],
    )
    refined = CommitmentExtraction(
        kind="task",
        summary="לקבוע תור לרופא שיניים",
        action_type="other",
        confidence=0.98,
        evidence="לקבוע תור — רופא שיניים",
    )
    app = app_factory(
        [
            ExtractionResult(language="he", items=[initial]),
            ExtractionResult(language="he", items=[refined]),
        ]
    )
    async for client in client_for_app(app):
        body = await _post_shortcut(client, "לקבוע תור", fixed_now)
        approval_id = uuid.UUID(body["approval_ids"][0])
        notifier = cast(FakeTelegramNotifier, app.state.notifier)
        assert notifier.notifications[-1].kind == "detail_clarification_request"

        intake = cast(IntakeService, app.state.intake_service)
        resolution = await intake.refine_approval(approval_id, "רופא שיניים")
        assert resolution.state == "executed"

        factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            task = (await session.scalars(select(Task))).one()
            assert approval is not None and approval.status is ApprovalStatus.EXECUTED
            assert task.title == "לקבוע תור לרופא שיניים"


async def test_recent_similar_task_in_same_conversation_is_superseded(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    first = CommitmentExtraction(
        kind="task",
        summary="לקבוע שיחה עם האוניברסיטה הפתוחה",
        action_type="call",
        confidence=0.99,
        evidence="לקבוע שיחה עם האוניברסיטה הפתוחה",
    )
    second = first.model_copy(
        update={
            "summary": "לקבוע שיחת ייעוץ עם האוניברסיטה הפתוחה",
            "evidence": "לקבוע שיחת ייעוץ עם האוניברסיטה הפתוחה",
        }
    )
    app = app_factory(
        [
            ExtractionResult(language="he", items=[first]),
            ExtractionResult(language="he", items=[second]),
        ]
    )
    async for client in client_for_app(app):
        await _post_shortcut(client, first.evidence, fixed_now)
        await _post_shortcut(client, second.evidence, fixed_now + timedelta(seconds=30))

        factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with factory() as session:
            tasks = list((await session.scalars(select(Task).order_by(Task.created_at))).all())
            approvals = list((await session.scalars(select(ApprovalRequest))).all())
            assert [task.status for task in tasks] == [
                TaskStatus.CANCELLED,
                TaskStatus.PENDING,
            ]
            assert tasks[-1].title == second.summary
            assert any(
                approval.action_type == DETAIL_CLARIFICATION_ACTION
                or approval.status is ApprovalStatus.REJECTED
                for approval in approvals
            )


async def test_rewrite_followup_skips_commitment_extraction_call(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
    fixed_now: datetime,
) -> None:
    app = app_factory([])
    async for client in client_for_app(app):
        body = await _post_shortcut(client, "תנכל לכנתב את זה יותר יפה", fixed_now)

        assert body["approval_ids"] == []
        llm = cast(FakeLLMProvider, app.state.llm_provider)
        assert llm.requests == []
