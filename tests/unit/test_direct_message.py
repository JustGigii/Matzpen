import uuid
from datetime import UTC, datetime
from pathlib import Path

from personal_agent.domain.database import Base, create_engine, create_session_factory
from personal_agent.domain.enums import ApprovalStatus
from personal_agent.domain.models import ApprovalRequest
from personal_agent.domain.schemas import CommitmentExtraction, ExtractionResult
from personal_agent.integrations.llm.fake import FakeLLMProvider
from personal_agent.integrations.telegram.fake import FakeTelegramNotifier
from personal_agent.services.confirmations import ConfirmationService
from personal_agent.services.direct_message import DirectMessageService, render_extraction


def test_render_extraction_uses_local_time() -> None:
    result = ExtractionResult(
        language="he",
        items=[
            CommitmentExtraction(
                kind="commitment",
                summary="Call Daniel",
                action_type="call",
                due_at="2026-08-02T11:15:00+00:00",
                confidence=0.97,
                evidence="explicit promise",
                requires_user_confirmation=True,
            )
        ],
    )

    rendered = render_extraction(result, "Asia/Jerusalem")

    assert "שיחה עם Daniel" in rendered
    assert "02.08 בשעה 14:15" in rendered
    assert "ביטחון" not in rendered
    assert "נדרש אישור" in rendered


async def test_direct_message_is_processed_and_sent_to_fake_telegram() -> None:
    extraction = ExtractionResult(language="en", items=[])
    llm = FakeLLMProvider([extraction])
    notifier = FakeTelegramNotifier()
    service = DirectMessageService(
        llm,
        notifier,
        "Asia/Jerusalem",
        lambda: datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
    )

    result, message_id = await service.process_and_notify("Remember this")

    assert result == extraction
    assert message_id == "fake-1"
    assert notifier.notifications[0].kind == "text"
    assert "לא זוהו" in notifier.notifications[0].text


async def test_ambiguous_extraction_has_persisted_confirmation_and_button(
    tmp_path: Path,
) -> None:
    database_path = str(tmp_path / "confirmations.db")
    engine = create_engine(f"sqlite+aiosqlite:///{database_path}")
    session_factory = create_session_factory(engine)

    def now() -> datetime:
        return datetime(2026, 8, 1, 10, 0, tzinfo=UTC)

    extraction = ExtractionResult(
        language="he",
        items=[
            CommitmentExtraction(
                kind="commitment",
                summary="Call Daniel",
                action_type="call",
                confidence=0.55,
                evidence="time is unclear",
                requires_user_confirmation=True,
            )
        ],
    )
    notifier = FakeTelegramNotifier()
    confirmation_service = ConfirmationService(session_factory, now)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        service = DirectMessageService(
            FakeLLMProvider([extraction]),
            notifier,
            "Asia/Jerusalem",
            now,
            confirmation_service,
        )

        await service.process_and_notify("Call Daniel sometime")

        approval_id = uuid.UUID(notifier.notifications[0].reference_id)
        assert "נדרש אישור" in notifier.notifications[0].text
        async with session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            assert approval is not None
            assert approval.status is ApprovalStatus.PENDING
        assert await confirmation_service.resolve(approval_id, approve=True) is True
        assert await confirmation_service.resolve(approval_id, approve=True) is False
        async with session_factory() as session:
            resolved = await session.get(ApprovalRequest, approval_id)
            assert resolved is not None
            assert resolved.status is ApprovalStatus.APPROVED
    finally:
        await engine.dispose()
