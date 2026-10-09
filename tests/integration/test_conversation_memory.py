from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from personal_agent.domain.database import Base, create_engine, create_session_factory
from personal_agent.domain.enums import (
    ActionType,
    ApprovalStatus,
    CommitmentDirection,
    CommitmentStatus,
    EventDirection,
    EventSource,
    MemoryStatus,
    ProcessingStatus,
    Sensitivity,
)
from personal_agent.domain.models import ApprovalRequest, Commitment, Event, MemoryFact
from personal_agent.domain.schemas import ChatResponse, MemoryCandidate
from personal_agent.integrations.google_calendar.base import CalendarEvent
from personal_agent.integrations.google_calendar.fake import FakeCalendarProvider
from personal_agent.integrations.llm.fake import FakeLLMProvider
from personal_agent.integrations.telegram.fake import FakeTelegramNotifier
from personal_agent.services.conversation import ConversationService


async def test_chat_uses_bounded_personal_context_and_memory_requires_approval(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 16, 0, tzinfo=UTC)
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'conversation.db').as_posix()}")
    factory = create_session_factory(engine)
    llm = FakeLLMProvider(
        chat_responses=[
            ChatResponse(
                reply="כן, אעזור לך לתכנן את השבוע.",
                memory_candidates=[
                    MemoryCandidate(
                        category="preference",
                        subject="המשתמש",
                        predicate="מעדיף זמן עבודה",
                        value="בבוקר",
                        confidence=0.95,
                    )
                ],
            )
        ]
    )
    notifier = FakeTelegramNotifier()
    calendar = FakeCalendarProvider(
        [
            CalendarEvent(
                external_id="calendar-1",
                summary="פגישה עם Shaked",
                start=now + timedelta(days=1),
                end=now + timedelta(days=1, hours=1),
            )
        ]
    )
    service = ConversationService(
        factory,
        llm,
        notifier,
        calendar,
        lambda: now,
        "Asia/Jerusalem",
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            source = Event(
                source=EventSource.TELEGRAM,
                source_account="test-bot",
                external_id="42",
                event_type="message.received",
                direction=EventDirection.INBOUND,
                occurred_at=now,
                received_at=now,
                actor_external_id="user-1",
                actor_display_name="Gigi",
                conversation_external_id="chat-1",
                content_text="תעזור לי לתכנן את השבוע",
                payload_json={},
                dedupe_key="chat-1:42",
                sensitivity=Sensitivity.PERSONAL,
                processing_status=ProcessingStatus.PROCESSED,
            )
            session.add(source)
            await session.flush()
            session.add(
                MemoryFact(
                    category="identity",
                    subject="המשתמש",
                    predicate="אזור זמן",
                    value_json={"value": "Asia/Jerusalem"},
                    status=MemoryStatus.CONFIRMED,
                    confidence=1.0,
                    sensitivity=Sensitivity.NORMAL,
                    source_event_ids=[str(source.id)],
                    valid_from=now,
                    last_verified_at=now,
                )
            )
            session.add(
                Commitment(
                    direction=CommitmentDirection.USER_PROMISED,
                    action_type=ActionType.MEET,
                    summary="שיחת Zoom עם Shaked",
                    due_at=now + timedelta(days=1),
                    source_event_id=source.id,
                    status=CommitmentStatus.SCHEDULED,
                    confidence=1.0,
                    reminder_lead_minutes=10,
                    dedupe_key="commitment-1",
                )
            )
            session.add(
                ApprovalRequest(
                    action_type="clarify_extraction",
                    action_payload={
                        "item": {
                            "summary": "לברר לגבי שיעור הכנה למבחן",
                            "evidence": "יהיה שיעור הכנה למבחן?",
                        },
                        "conversation_context": {
                            "type": "group",
                            "display_name": "אלגוריתמים Univeli",
                        },
                    },
                    risk_class="internal_reversible",
                    status=ApprovalStatus.EXECUTED,
                    source_event_id=source.id,
                    dedupe_key="recent-alert-context",
                )
            )
            await session.commit()
            source_id = source.id

        response = await service.respond(
            source_id,
            "chat-1",
            "תעזור לי לתכנן את השבוע",
        )

        assert response.reply == "כן, אעזור לך לתכנן את השבוע."
        request = llm.chat_requests[0]
        assert request.confirmed_memories[0].value == "Asia/Jerusalem"
        assert any("שיחת Zoom עם Shaked" in item for item in request.active_items)
        assert any("פגישה עם Shaked" in item for item in request.upcoming_calendar)
        assert any(
            "אלגוריתמים Univeli" in item and "יהיה שיעור הכנה למבחן?" in item
            for item in request.recent_notifications
        )
        assert [item.kind for item in notifier.notifications] == ["text", "memory_request"]

        async with factory() as session:
            proposal = (
                await session.scalars(
                    select(MemoryFact).where(MemoryFact.status == MemoryStatus.PROPOSED)
                )
            ).one()
            approval = (
                await session.scalars(
                    select(ApprovalRequest).where(ApprovalRequest.status == ApprovalStatus.PENDING)
                )
            ).one()
            assistant_reply = (
                await session.scalars(select(Event).where(Event.event_type == "assistant.reply"))
            ).one()
            assert assistant_reply.content_text == response.reply
            proposal_id = proposal.id
            approval_id = approval.id

        assert await service.resolve_memory(approval_id, remember=True) is True
        assert await service.resolve_memory(approval_id, remember=True) is False
        async with factory() as session:
            confirmed = await session.get(MemoryFact, proposal_id)
            resolved = await session.get(ApprovalRequest, approval_id)
            assert confirmed is not None and confirmed.status is MemoryStatus.CONFIRMED
            assert resolved is not None and resolved.status is ApprovalStatus.EXECUTED
    finally:
        await engine.dispose()


def test_memory_card_is_localized_and_transient_work_is_rejected() -> None:
    preference = MemoryFact(
        category="preference",
        subject="user",
        predicate="prefers language",
        value_json={"value": "Hebrew"},
        status=MemoryStatus.PROPOSED,
        confidence=0.95,
        sensitivity=Sensitivity.PERSONAL,
        source_event_ids=[],
        last_verified_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    transient = MemoryCandidate(
        category="commitment",
        subject="user",
        predicate="has commitment",
        value="לקבוע שיחה",
        confidence=0.99,
    )

    assert ConversationService._memory_summary(preference) == (
        "👤 המשתמש\n💡 שפה מועדפת: עברית\n🏷️ קטגוריה: העדפה"
    )
    assert ConversationService._is_durable_memory_candidate(transient) is False
