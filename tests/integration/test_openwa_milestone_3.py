import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.core.config import Settings
from personal_agent.domain.enums import (
    ApprovalStatus,
    EventDirection,
    HistoricalFindingStatus,
    WhatsAppBufferStatus,
    WhatsAppInitialReviewStatus,
)
from personal_agent.domain.models import (
    ApprovalRequest,
    Event,
    Person,
    Task,
    WhatsAppConversationBuffer,
    WhatsAppHistoricalFinding,
    WhatsAppSessionState,
)
from personal_agent.domain.schemas import CommitmentExtraction, ExtractionResult
from personal_agent.integrations.llm.fake import FakeLLMProvider
from personal_agent.integrations.openwa.client import HttpOpenWAReadClient, OpenWAMediaContent
from personal_agent.integrations.openwa.fake import FakeOpenWAReadClient
from personal_agent.integrations.openwa.schemas import OpenWAEventData, OpenWAHistoryMessage
from personal_agent.integrations.openwa.signature import OpenWASignatureVerifier
from personal_agent.integrations.telegram.fake import FakeTelegramNotifier
from personal_agent.main import create_app
from personal_agent.services.whatsapp import WhatsAppService


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def extraction(*, kind: str = "task", summary: str = "שליחת הקובץ") -> ExtractionResult:
    return ExtractionResult(
        language="he",
        items=[
            CommitmentExtraction(
                kind=kind,
                summary=summary,
                action_type="message",
                due_at="2026-08-02T09:00:00+03:00",
                confidence=0.97,
                evidence="בקשה מפורשת",
            )
        ],
    )


def make_payload(
    message_id: str,
    body: str | None,
    *,
    event: str = "message.received",
    chat_id: str = "private-chat",
    from_me: bool | None = False,
    timestamp: str = "2026-08-01T12:00:00+03:00",
    **data_overrides: object,
) -> dict[str, object]:
    data: dict[str, object] = {
        "id": message_id,
        "chatId": chat_id,
        "senderId": "self" if from_me else "contact-1",
        "senderName": "אני" if from_me else "דניאל",
        "timestamp": timestamp,
        "body": body,
    }
    if from_me is not None:
        data["fromMe"] = from_me
    data.update(data_overrides)
    return {"event": event, "sessionId": "fake-session", "data": data}


def build_app(
    database_path: Path,
    clock: MutableClock,
    *,
    results: list[ExtractionResult] | None = None,
    media_texts: list[str] | None = None,
    openwa_client: FakeOpenWAReadClient | None = None,
    configured: bool = False,
    ignored_chat_ids: tuple[str, ...] = (),
    webhook_max_bytes: int = 1_048_576,
) -> FastAPI:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        auto_create_schema=True,
        openwa_webhook_secret="test-webhook-secret",
        openwa_webhook_max_bytes=webhook_max_bytes,
        openwa_api_key="fake-api-key" if configured else None,
        openwa_session_id="fake-session" if configured else None,
        whatsapp_ignored_chat_ids=ignored_chat_ids,
        proactive_check_interval_seconds=3600,
        internal_action_grace_seconds=60,
        gemini_api_key=None,
        gemini_model=None,
        telegram_bot_token=None,
        telegram_allowed_user_ids=(),
        shortcut_bearer_token="test-shortcut-token",
        google_client_secret_file=None,
        google_token_file=None,
    )
    app = create_app(
        settings=settings,
        llm_provider=FakeLLMProvider(results, media_texts=media_texts),
        notifier=FakeTelegramNotifier(),
        openwa_client=openwa_client,
        clock=clock,
    )
    app.state.control.pause()
    return app


@asynccontextmanager
async def app_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def signed_post(client: AsyncClient, payload: dict[str, object]) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    signature = OpenWASignatureVerifier("test-webhook-secret").sign(body)
    return await client.post(
        "/api/webhooks/openwa",
        content=body,
        headers={"Content-Type": "application/json", "X-OpenWA-Signature": signature},
    )


async def test_openwa_test_webhook_is_authenticated_and_accepted(tmp_path: Path) -> None:
    app = build_app(
        tmp_path / "openwa-test-webhook.db",
        MutableClock(datetime(2026, 8, 2, 12, 0, tzinfo=UTC)),
    )
    payload = {
        "event": "test",
        "timestamp": "2026-08-02T12:00:00Z",
        "sessionId": "fake-session",
        "deliveryId": "test-delivery",
        "data": {"message": "This is a test webhook from OpenWA"},
    }

    async with app_client(app) as client:
        response = await signed_post(client, payload)

    assert response.status_code == 202
    assert response.json() == {
        "accepted": False,
        "duplicate": False,
        "buffered": False,
        "ignored_reason": "unsupported_event",
        "event_id": None,
        "buffer_id": None,
    }


async def test_inbound_batch_resets_timer_recovers_after_restart_and_stays_proposal_only(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "restart.db"
    start = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    clock = MutableClock(start)
    first_app = build_app(database_path, clock)

    async with app_client(first_app) as client:
        first = await signed_post(client, make_payload("in-1", "תשלח לי מחר את הקובץ"))
        clock.value = start + timedelta(minutes=2)
        second = await signed_post(client, make_payload("in-2", "זה חשוב לפגישה מחר"))
        assert first.json()["buffer_id"] == second.json()["buffer_id"]

        whatsapp = cast(WhatsAppService, first_app.state.whatsapp_service)
        assert await whatsapp.flush_due_buffers(start + timedelta(minutes=3)) == 0
        session_factory = cast(async_sessionmaker[AsyncSession], first_app.state.session_factory)
        async with session_factory() as session:
            raw_events = list(
                (
                    await session.scalars(
                        select(Event).where(Event.event_type == "message.received")
                    )
                ).all()
            )
            buffer = (await session.scalars(select(WhatsAppConversationBuffer))).one()
            assert {event.direction for event in raw_events} == {EventDirection.INBOUND}
            assert len(buffer.event_ids) == 2
            assert buffer.flush_at == start + timedelta(minutes=4)

    clock.value = start + timedelta(minutes=4)
    second_app = build_app(database_path, clock, results=[extraction()])
    async with app_client(second_app):
        whatsapp = cast(WhatsAppService, second_app.state.whatsapp_service)
        assert await whatsapp.flush_due_buffers() == 1
        session_factory = cast(async_sessionmaker[AsyncSession], second_app.state.session_factory)
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(Task)) == 0
            approval = (await session.scalars(select(ApprovalRequest))).one()
            assert approval.status is ApprovalStatus.PENDING
            assert approval.execute_after is None
            person = (await session.scalars(select(Person))).one()
            assert person.external_id == "contact-1"
            assert person.display_name == "דניאל"
            assert person.operational_facts["requests_to_user"] == 1
            assert person.operational_facts["open_follow_ups"] == 1
        llm = cast(FakeLLMProvider, second_app.state.llm_provider)
        assert "תשלח לי מחר את הקובץ" in llm.requests[0].content_text
        assert "זה חשוב לפגישה מחר" in llm.requests[0].content_text


async def test_urgent_outgoing_bypasses_buffer_without_duplicate_and_normalizes_direction(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    clock = MutableClock(start)
    app = build_app(
        tmp_path / "urgent.db",
        clock,
        results=[extraction(kind="commitment", summary="לחזור לדניאל")],
    )
    payload = make_payload(
        "urgent-1",
        "אחזור אליך בעוד 10 דקות",
        event="message.sent",
        from_me=None,
    )

    async with app_client(app) as client:
        first = await signed_post(client, payload)
        whatsapp = cast(WhatsAppService, app.state.whatsapp_service)
        await whatsapp.wait_for_background_work()
        duplicate = await signed_post(client, payload)
        assert first.status_code == 202
        assert first.json()["buffered"] is True
        assert duplicate.json()["duplicate"] is True

        assert await whatsapp.flush_due_buffers(start + timedelta(hours=1)) == 0
        session_factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with session_factory() as session:
            raw = (
                await session.scalars(select(Event).where(Event.event_type == "message.sent"))
            ).one()
            assert raw.direction is EventDirection.OUTBOUND
            assert await session.scalar(select(func.count()).select_from(ApprovalRequest)) == 1
        notifier = cast(FakeTelegramNotifier, app.state.notifier)
        assert len(notifier.notifications) == 1
        llm = cast(FakeLLMProvider, app.state.llm_provider)
        assert len(llm.requests) == 1


async def test_urgent_colloquial_outgoing_with_common_typos_is_not_filtered(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 2, 11, 16, tzinfo=UTC)
    app = build_app(
        tmp_path / "urgent-colloquial.db",
        MutableClock(start),
        results=[extraction(kind="commitment", summary="להתקשר בעוד 10 דקות")],
    )
    payload = make_payload(
        "urgent-colloquial-1",
        "אני עןד 10 דקות התקשר אילך",
        event="message.sent",
        from_me=None,
    )

    async with app_client(app) as client:
        response = await signed_post(client, payload)
        whatsapp = cast(WhatsAppService, app.state.whatsapp_service)

        assert response.status_code == 202
        assert response.json()["buffered"] is True
        await whatsapp.wait_for_background_work()

        session_factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with session_factory() as session:
            approval = (await session.scalars(select(ApprovalRequest))).one()
            assert approval.status is ApprovalStatus.PENDING
        llm = cast(FakeLLMProvider, app.state.llm_provider)
        assert "אני עןד 10 דקות התקשר אילך" in llm.requests[0].content_text


async def test_private_chat_context_is_passed_to_the_extractor(tmp_path: Path) -> None:
    start = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    openwa_client = FakeOpenWAReadClient(chat_display_names={"mom-chat": "\u05d0\u05de\u05d0"})
    app = build_app(
        tmp_path / "private-chat-context.db",
        MutableClock(start),
        results=[
            extraction(
                kind="commitment", summary="\u05d0\u05d7\u05d6\u05d5\u05e8 \u05dc\u05d0\u05de\u05d0"
            )
        ],
        openwa_client=openwa_client,
    )
    payload = make_payload(
        "mom-context-1",
        (
            "\u05d0\u05d7\u05d6\u05d5\u05e8 \u05d0\u05dc\u05d9\u05d9\u05da "
            "\u05d1\u05e2\u05d5\u05d3 10 \u05d3\u05e7\u05d5\u05ea"
        ),
        event="message.sent",
        from_me=None,
        chat_id="mom-chat",
        chatName=None,
    )

    async with app_client(app) as client:
        response = await signed_post(client, payload)
        whatsapp = cast(WhatsAppService, app.state.whatsapp_service)

        assert response.status_code == 202
        await whatsapp.wait_for_background_work()

    llm = cast(FakeLLMProvider, app.state.llm_provider)
    assert llm.requests[0].conversation_display_name == "\u05d0\u05de\u05d0"
    assert llm.requests[0].conversation_type == "private"
    notifier = cast(FakeTelegramNotifier, app.state.notifier)
    assert (
        "\u05e9\u05d9\u05d7\u05ea WhatsApp \u05e2\u05dd \u05d0\u05de\u05d0"
        in notifier.notifications[0].text
    )


async def test_group_archive_and_denylist_rules_are_local_and_deterministic(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 1, 9, 0, tzinfo=UTC))
    app = build_app(tmp_path / "filters.db", clock, ignored_chat_ids=("denied-chat",))

    async with app_client(app) as client:
        unrelated_group = await signed_post(
            client,
            make_payload(
                "group-1",
                "תשלח את הקובץ מחר",
                chat_id="team@g.us",
                isGroup=True,
            ),
        )
        mentioned_group = await signed_post(
            client,
            make_payload(
                "group-2",
                "דניאל, תשלח לי מחר את הקובץ",
                chat_id="team@g.us",
                isGroup=True,
                userMentioned=True,
            ),
        )
        archived = await signed_post(
            client,
            make_payload(
                "archived-1",
                "תשלח לי מחר את הקובץ",
                chat_id="archived-chat",
                isArchived=True,
            ),
        )
        denied = await signed_post(
            client,
            make_payload("denied-1", "תשלח לי מחר את הקובץ", chat_id="denied-chat"),
        )

        assert unrelated_group.json()["ignored_reason"] == "unaddressed_group_message"
        assert mentioned_group.json()["buffered"] is True
        assert archived.json()["ignored_reason"] == "archived_chat"
        assert denied.json()["ignored_reason"] == "manual_denylist"
        llm = cast(FakeLLMProvider, app.state.llm_provider)
        assert llm.requests == []


async def test_initial_seven_day_history_is_capped_review_only_and_watermarked(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    included_at = now - timedelta(days=2)
    archived_at = now - timedelta(days=1)
    read_client = FakeOpenWAReadClient(
        archived_chat_ids={"archived-chat"},
        history=[
            OpenWAHistoryMessage(
                session_id="fake-session",
                data=OpenWAEventData(
                    id="history-1",
                    chatId="history-chat",
                    senderId="contact-1",
                    senderName="דניאל",
                    timestamp=included_at,
                    body="תשלח לי מחר את הקובץ",
                ),
            ),
            OpenWAHistoryMessage(
                session_id="fake-session",
                data=OpenWAEventData(
                    id="history-archived",
                    chatId="archived-chat",
                    timestamp=archived_at,
                    body="תזכיר לי פגישה מחר",
                ),
            ),
            OpenWAHistoryMessage(
                session_id="fake-session",
                data=OpenWAEventData(
                    id="history-old",
                    chatId="history-chat",
                    timestamp=now - timedelta(days=8),
                    body="תזכיר לי פגישה מחר",
                ),
            ),
        ],
    )
    app = build_app(
        tmp_path / "history.db",
        MutableClock(now),
        results=[extraction()],
        openwa_client=read_client,
        configured=True,
    )

    async with app_client(app):
        whatsapp = cast(WhatsAppService, app.state.whatsapp_service)
        assert await whatsapp.run_initial_history_review() == 1
        assert await whatsapp.run_initial_history_review() == 0
        session_factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(Task)) == 0
            finding = (await session.scalars(select(WhatsAppHistoricalFinding))).one()
            assert finding.status is HistoricalFindingStatus.PENDING
            state = await session.get(WhatsAppSessionState, "fake-session")
            assert state is not None
            assert state.initial_review_status is WhatsAppInitialReviewStatus.COMPLETED
            assert state.history_watermark == archived_at
        assert read_client.history_requests[0][2:] == (3000, 300)


async def test_unavailable_archive_review_warning_is_deduplicated_and_retries_on_cooldown(
    tmp_path: Path,
) -> None:
    class UnavailableArchiveClient(FakeOpenWAReadClient):
        def __init__(self) -> None:
            super().__init__()
            self.archive_requests = 0

        async def archived_chat_ids(self, session_id: str) -> set[str]:
            del session_id
            self.archive_requests += 1
            raise RuntimeError("archive state unavailable")

    now = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    clock = MutableClock(now)
    read_client = UnavailableArchiveClient()
    app = build_app(
        tmp_path / "archive-unavailable.db",
        clock,
        openwa_client=read_client,
        configured=True,
    )

    async with app_client(app):
        whatsapp = cast(WhatsAppService, app.state.whatsapp_service)
        notifier = cast(FakeTelegramNotifier, app.state.notifier)

        assert await whatsapp.run_initial_history_review() == 0
        requests_after_failure = read_client.archive_requests
        assert len(notifier.notifications) == 1

        assert await whatsapp.run_initial_history_review() == 0
        assert read_client.archive_requests == requests_after_failure
        assert len(notifier.notifications) == 1

        clock.value = now + timedelta(minutes=31)
        assert await whatsapp.run_initial_history_review() == 0
        assert read_client.archive_requests > requests_after_failure
        assert len(notifier.notifications) == 1


async def test_edit_replaces_pending_source_and_revocation_cancels_pending_buffer(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    app = build_app(
        tmp_path / "changes.db",
        MutableClock(start),
        results=[ExtractionResult(language="he", items=[])],
    )

    async with app_client(app) as client:
        await signed_post(
            client,
            make_payload(
                "original-edit",
                "אשלח לך את הקובץ מחר",
                event="message.sent",
                from_me=True,
            ),
        )
        edited = make_payload(
            "edit-1",
            "אשלח לך את המצגת מחר",
            event="message.edited",
            from_me=True,
            originalMessageId="original-edit",
        )
        assert (await signed_post(client, edited)).json()["buffered"] is True

        whatsapp = cast(WhatsAppService, app.state.whatsapp_service)
        assert await whatsapp.flush_due_buffers(start + timedelta(minutes=5)) == 1
        llm = cast(FakeLLMProvider, app.state.llm_provider)
        assert "המצגת" in llm.requests[0].content_text
        assert "הקובץ" not in llm.requests[0].content_text

        await signed_post(
            client,
            make_payload(
                "original-revoke",
                "אשלח לך מסמך מחר",
                event="message.sent",
                from_me=True,
            ),
        )
        revoked = make_payload(
            "revoke-1",
            None,
            event="message.revoked",
            from_me=True,
            originalMessageId="original-revoke",
        )
        response = await signed_post(client, revoked)
        assert response.json()["ignored_reason"] == "revocation_recorded"
        assert await whatsapp.flush_due_buffers(start + timedelta(hours=1)) == 0

        session_factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with session_factory() as session:
            original = (
                await session.scalars(select(Event).where(Event.external_id == "original-revoke"))
            ).one()
            assert original.revoked_at == start
            buffers = list((await session.scalars(select(WhatsAppConversationBuffer))).all())
            assert any(buffer.status is WhatsAppBufferStatus.CANCELLED for buffer in buffers)


async def test_supported_media_is_transient_video_and_oversize_are_rejected_and_content_expires(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    read_client = FakeOpenWAReadClient(
        media={
            "voice-1": OpenWAMediaContent(
                content=b"fake-voice-bytes",
                mime_type="audio/ogg",
                filename="voice.ogg",
            )
        }
    )
    app = build_app(
        tmp_path / "media.db",
        MutableClock(now),
        results=[ExtractionResult(language="he", items=[])],
        media_texts=["תזכיר לי פגישה מחר"],
        openwa_client=read_client,
    )

    async with app_client(app) as client:
        voice = make_payload(
            "voice-1",
            None,
            messageType="voice",
            media={"type": "voice", "mimeType": "audio/ogg", "size": 16},
        )
        video = make_payload(
            "video-1",
            None,
            messageType="video",
            media={"type": "video", "mimeType": "video/mp4", "size": 20},
        )
        oversized = make_payload(
            "large-1",
            None,
            messageType="document",
            media={"type": "document", "mimeType": "application/pdf", "size": 19_000_000},
        )
        assert (await signed_post(client, voice)).json()["buffered"] is True
        assert (await signed_post(client, video)).json()["ignored_reason"] == "video_not_supported"
        assert (await signed_post(client, oversized)).json()["ignored_reason"] == "media_too_large"

        whatsapp = cast(WhatsAppService, app.state.whatsapp_service)
        assert await whatsapp.flush_due_buffers(now + timedelta(minutes=5)) == 1
        assert read_client.media_requests == [("fake-session", "voice-1")]
        llm = cast(FakeLLMProvider, app.state.llm_provider)
        assert llm.media_requests == [("audio/ogg", "voice.ogg", 16)]
        assert "תזכיר לי פגישה מחר" in llm.requests[0].content_text

        expired = make_payload(
            "expired-1",
            "תזכיר לי פגישה מחר",
            timestamp="2026-06-30T12:00:00+03:00",
        )
        await signed_post(client, expired)
        assert await whatsapp.redact_expired_events() == 1
        session_factory = cast(async_sessionmaker[AsyncSession], app.state.session_factory)
        async with session_factory() as session:
            stored_voice = (
                await session.scalars(select(Event).where(Event.external_id == "voice-1"))
            ).one()
            assert (
                b"fake-voice-bytes"
                not in json.dumps(stored_voice.payload_json, ensure_ascii=False).encode()
            )
            expired_event = (
                await session.scalars(select(Event).where(Event.external_id == "expired-1"))
            ).one()
            assert expired_event.content_text is None
            assert expired_event.payload_json["redacted"] is True
            assert expired_event.dedupe_key


async def test_connectivity_status_warning_body_limit_and_read_only_surface(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    clock = MutableClock(start)
    read_client = FakeOpenWAReadClient(status="disconnected")
    app = build_app(
        tmp_path / "status.db",
        clock,
        openwa_client=read_client,
        configured=True,
        webhook_max_bytes=1024,
    )

    async with app_client(app) as client:
        disconnected_payload = {
            "event": "session.disconnected",
            "sessionId": "fake-session",
            "data": {"status": "disconnected"},
        }
        await signed_post(client, disconnected_payload)
        await signed_post(client, disconnected_payload)
        notifier = cast(FakeTelegramNotifier, app.state.notifier)
        assert len(notifier.notifications) == 1

        whatsapp = cast(WhatsAppService, app.state.whatsapp_service)
        clock.value = start + timedelta(minutes=9)
        assert await whatsapp.send_disconnect_warning_if_due() == 0
        clock.value = start + timedelta(minutes=10)
        assert await whatsapp.send_disconnect_warning_if_due() == 1
        assert await whatsapp.send_disconnect_warning_if_due() == 0

        status = (await client.get("/api/status")).json()["whatsapp"]
        assert status["configured"] is True
        assert status["reachable"] is True
        assert status["session_state"] == "disconnected"
        assert status["disconnected_seconds"] == 600
        assert status["relink_required"] is True

        connected_payload = {
            "event": "session.status",
            "sessionId": "fake-session",
            "data": {"status": "connected"},
        }
        await signed_post(client, connected_payload)
        assert len(notifier.notifications) == 3

        oversized_body = b"{" + b"x" * 1024 + b"}"
        signature = OpenWASignatureVerifier("test-webhook-secret").sign(oversized_body)
        response = await client.post(
            "/api/webhooks/openwa",
            content=oversized_body,
            headers={"X-OpenWA-Signature": signature},
        )
        assert response.status_code == 413

    forbidden_methods = {"send_message", "reply", "react", "delete_message"}
    assert all(not hasattr(read_client, method) for method in forbidden_methods)
    http_client = HttpOpenWAReadClient("http://127.0.0.1:2785/api", "fake-key")
    try:
        assert all(not hasattr(http_client, method) for method in forbidden_methods)
    finally:
        await http_client.aclose()
    remote_https_client = HttpOpenWAReadClient("https://openwa.example.test/api", "fake-key")
    try:
        assert all(not hasattr(remote_https_client, method) for method in forbidden_methods)
    finally:
        await remote_https_client.aclose()
