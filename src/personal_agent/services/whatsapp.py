import asyncio
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.core.config import Settings
from personal_agent.core.time import require_aware
from personal_agent.domain.enums import (
    ApprovalStatus,
    CommitmentStatus,
    EventDirection,
    EventSource,
    HistoricalFindingStatus,
    ProcessingStatus,
    TaskStatus,
    WhatsAppBufferStatus,
    WhatsAppConversationType,
    WhatsAppInitialReviewStatus,
    WhatsAppSessionStatus,
)
from personal_agent.domain.models import (
    ApprovalRequest,
    AuditLog,
    Commitment,
    Event,
    Person,
    Task,
    WhatsAppConversation,
    WhatsAppConversationBuffer,
    WhatsAppHistoricalFinding,
    WhatsAppSessionState,
)
from personal_agent.domain.schemas import IntakeResult, NormalizedEvent
from personal_agent.integrations.openwa.client import OpenWAReadClient
from personal_agent.integrations.openwa.schemas import (
    OpenWAEventData,
    OpenWAWebhook,
    OpenWAWebhookResponse,
)
from personal_agent.integrations.telegram.base import TelegramNotifier
from personal_agent.repositories.events import EventRepository
from personal_agent.services.intake import IntakeService
from personal_agent.services.media import MediaAttachment, MediaTextService, MediaValidationError

MESSAGE_EVENTS = {
    "message.received",
    "message.sent",
    "message.edited",
    "message.revoked",
    "message.failed",
}
SESSION_EVENTS = {
    "session.status",
    "session.disconnected",
    "session.authenticated",
    "session.qr",
}
SUPPORTED_MEDIA_TYPES = {
    "audio",
    "voice",
    "image",
    "document",
    "pdf",
    "docx",
    "text",
}
IRRELEVANT_TEXT = re.compile(r"^(?:[😂🤣😁😀🙂👍🙏❤️❤]+|חח+(?:ה+)?|lol+|ok|סבבה)$", re.I)
RELEVANT_TEXT = re.compile(
    r"(?:\b\d{1,2}[:.]\d{2}\b|\bמחר\b|\bהיום\b|\bdeadline\b|\bmeeting\b|"
    r"\binterview\b|\bsubmit\b|\bcall\b|\bappointment\b|תזכיר|דדליין|להגיש|פגישה|"
    r"ראיון|שיעור|תחזור|אחזור|אתקשר|אשלח|אקבע|אעשה|תשלח|תתקשר|צריך|אפשר)",
    re.I,
)
RELATIVE_MINUTES_TEXT = re.compile(
    r"(?:בעוד|עוד|עןד|in)\s+(\d{1,2})\s*(?:דקות?|דק['׳]?|minutes?)",
    re.I,
)
USER_COMMITMENT_TEXT = re.compile(
    r"(?:אחזור|אתקשר|אשלח|אקבע|אעשה|"
    r"אני(?:\s+\S+){0,5}\s+(?:אחזור|יחזור|יחוזר|אתקשר|יתקשר|התקשר|"
    r"אשלח|ישלח|אקבע|יקבע|אעשה|יעשה)|\bi(?:'ll| will)\b)",
    re.I,
)

logger = logging.getLogger(__name__)


class WhatsAppService:
    """Durable read-only WhatsApp ingress, batching, review, and maintenance."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        intake_service: IntakeService,
        notifier: TelegramNotifier,
        media_text_service: MediaTextService,
        settings: Settings,
        now: Callable[[], datetime],
        read_client: OpenWAReadClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._intake = intake_service
        self._notifier = notifier
        self._media = media_text_service
        self._settings = settings
        self._now = now
        self._read_client = read_client
        self._lock = asyncio.Lock()
        self._timezone = ZoneInfo(settings.timezone)
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def handle_webhook(self, webhook: OpenWAWebhook) -> OpenWAWebhookResponse:
        received_at = require_aware(self._now())
        await self._record_session_webhook(webhook, received_at)
        if webhook.event in SESSION_EVENTS:
            await self._apply_session_event(webhook, received_at)
            return OpenWAWebhookResponse(
                accepted=True, event_id=webhook.stable_event_id(received_at)
            )
        if webhook.event == "call.received":
            return await self._store_call_event(webhook, received_at)
        if webhook.event not in MESSAGE_EVENTS:
            return OpenWAWebhookResponse(accepted=False, ignored_reason="unsupported_event")

        data = (
            webhook.data.model_copy(update={"from_me": True})
            if webhook.event == "message.sent" and not webhook.data.from_me
            else webhook.data
        )
        if data is not webhook.data:
            webhook = webhook.model_copy(update={"data": data})
        if data.chat_id is None or data.stable_id is None:
            return OpenWAWebhookResponse(accepted=False, ignored_reason="missing_message_identity")

        async with self._lock, self._session_factory() as session:
            conversation = await self._upsert_conversation(session, webhook, received_at)
            event, created = await self._store_message_event(session, webhook, received_at)
            if not created:
                await session.commit()
                return OpenWAWebhookResponse(
                    accepted=True,
                    duplicate=True,
                    event_id=str(event.id),
                )

            if webhook.event in {"message.edited", "message.revoked"}:
                review_only = await self._handle_edit_or_revocation(
                    session,
                    event,
                    webhook,
                    received_at,
                )
                if webhook.event == "message.revoked" or review_only:
                    event.processing_status = ProcessingStatus.PROCESSED
                    await session.commit()
                    return OpenWAWebhookResponse(
                        accepted=True,
                        event_id=str(event.id),
                        ignored_reason=(
                            "revocation_recorded"
                            if not review_only
                            else "executed_item_requires_review"
                        ),
                    )

            ignored_reason = self._ignored_reason(conversation, data)
            if ignored_reason is not None:
                event.processing_status = ProcessingStatus.PROCESSED
                session.add(
                    AuditLog(
                        actor="whatsapp_service",
                        action="filter_whatsapp_event",
                        target=str(event.id),
                        source_event_id=event.id,
                        result="ignored",
                        redacted_metadata={"reason": ignored_reason},
                    )
                )
                await session.commit()
                return OpenWAWebhookResponse(
                    accepted=True,
                    event_id=str(event.id),
                    ignored_reason=ignored_reason,
                )

            await self._upsert_person(session, event, conversation, data)
            if self._is_urgent_user_commitment(data, event.occurred_at):
                buffer = await self._buffer_event(
                    session, conversation, event, received_at, urgent=True
                )
                await session.commit()
                self._schedule_immediate_flush()
                return OpenWAWebhookResponse(
                    accepted=True,
                    buffered=True,
                    event_id=str(event.id),
                    buffer_id=str(buffer.id),
                )

            buffer = await self._buffer_event(session, conversation, event, received_at)
            await session.commit()
            return OpenWAWebhookResponse(
                accepted=True,
                buffered=True,
                event_id=str(event.id),
                buffer_id=str(buffer.id),
            )

    async def stop(self) -> None:
        """Cancel any best-effort immediate flushes during application shutdown."""
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_for_background_work(self) -> None:
        """Wait for scheduled immediate processing (used by deterministic tests)."""
        tasks = tuple(self._background_tasks)
        if tasks:
            await asyncio.gather(*tasks)

    def _schedule_immediate_flush(self) -> None:
        """Process time-bound outbound commitments without delaying the webhook response."""
        task = asyncio.create_task(
            self._flush_due_buffers_in_background(),
            name="whatsapp-immediate-commitment-flush",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _flush_due_buffers_in_background(self) -> None:
        try:
            await self.flush_due_buffers()
        except Exception:
            logger.exception("Immediate WhatsApp commitment processing failed")

    async def flush_due_buffers(self, at: datetime | None = None) -> int:
        effective_at = require_aware(at or self._now())
        async with self._lock, self._session_factory() as session:
            buffers = list(
                (
                    await session.scalars(
                        select(WhatsAppConversationBuffer).where(
                            WhatsAppConversationBuffer.status.in_(
                                [WhatsAppBufferStatus.PENDING, WhatsAppBufferStatus.PROCESSING]
                            ),
                            WhatsAppConversationBuffer.flush_at <= effective_at,
                        )
                    )
                ).all()
            )
            buffer_ids = [buffer.id for buffer in buffers]
            for buffer in buffers:
                buffer.status = WhatsAppBufferStatus.PROCESSING
            await session.commit()

        processed = 0
        for buffer_id in buffer_ids:
            try:
                if await self._flush_buffer(buffer_id, effective_at):
                    processed += 1
            except Exception:
                async with self._session_factory() as session:
                    retry_buffer = await session.get(WhatsAppConversationBuffer, buffer_id)
                    if retry_buffer is not None:
                        retry_buffer.status = WhatsAppBufferStatus.PENDING
                        retry_buffer.flush_at = effective_at + timedelta(seconds=60)
                        session.add(
                            AuditLog(
                                actor="whatsapp_service",
                                action="flush_conversation_buffer",
                                target=str(retry_buffer.id),
                                result="retry_scheduled",
                            )
                        )
                        await session.commit()
                raise
        return processed

    async def _flush_buffer(self, buffer_id: uuid.UUID, effective_at: datetime) -> bool:
        async with self._session_factory() as session:
            buffer = await session.get(WhatsAppConversationBuffer, buffer_id)
            if buffer is None or buffer.status not in {
                WhatsAppBufferStatus.PENDING,
                WhatsAppBufferStatus.PROCESSING,
            }:
                return False
            conversation = await session.get(WhatsAppConversation, buffer.conversation_id)
            if conversation is None:
                buffer.status = WhatsAppBufferStatus.FAILED
                await session.commit()
                return False
            event_ids = [uuid.UUID(value) for value in buffer.event_ids]
            events = list(
                (await session.scalars(select(Event).where(Event.id.in_(event_ids)))).all()
            )
            events.sort(key=lambda item: item.occurred_at)
            if not events:
                buffer.status = WhatsAppBufferStatus.CANCELLED
                await session.commit()
                return False
            session_id = conversation.session_id
            batch_key = buffer.dedupe_key
            urgent = buffer.urgent

        await self._resolve_conversation_display_name(conversation)

        await self._process_events(
            session_id, conversation, events, batch_key=batch_key, urgent=urgent
        )

        async with self._session_factory() as session:
            stored_buffer = await session.get(WhatsAppConversationBuffer, buffer_id)
            conversation = await session.get(WhatsAppConversation, conversation.id)
            if stored_buffer is None or conversation is None:
                return False
            stored_buffer.status = WhatsAppBufferStatus.PROCESSED
            stored_buffer.processed_at = effective_at
            conversation.processing_watermark = max(event.occurred_at for event in events)
            for event_id in event_ids:
                event = await session.get(Event, event_id)
                if event is not None:
                    event.processing_status = ProcessingStatus.PROCESSED
            state = await session.get(WhatsAppSessionState, session_id)
            if state is not None:
                state.last_processed_event_at = effective_at
            session.add(
                AuditLog(
                    actor="whatsapp_service",
                    action="flush_conversation_buffer",
                    target=str(stored_buffer.id),
                    result="processed",
                    redacted_metadata={"event_count": len(events)},
                )
            )
            await session.commit()
        return True

    async def _resolve_conversation_display_name(self, conversation: WhatsAppConversation) -> None:
        if conversation.display_name or self._read_client is None:
            return
        display_name = await self._read_client.conversation_display_name(
            conversation.session_id, conversation.external_chat_id
        )
        if display_name is None:
            return
        conversation.display_name = display_name
        async with self._session_factory() as session:
            stored = await session.get(WhatsAppConversation, conversation.id)
            if stored is not None and not stored.display_name:
                stored.display_name = display_name
                await session.commit()

    async def _process_events(
        self,
        session_id: str,
        conversation: WhatsAppConversation,
        events: Sequence[Event],
        *,
        batch_key: str,
        urgent: bool = False,
    ) -> IntakeResult:
        lines: list[str] = []
        force_confirmation = False
        for event in events:
            content = event.content_text or ""
            media_text = await self._extract_event_media(session_id, event)
            if media_text:
                content = f"{content}\n{media_text}".strip()
            payload_data = event.payload_json.get("data")
            quoted = payload_data.get("quotedMessage") if isinstance(payload_data, dict) else None
            if event.direction is EventDirection.OUTBOUND and isinstance(quoted, dict):
                quoted_text = next(
                    (
                        value.strip()
                        for key in ("body", "text", "caption", "content")
                        if isinstance((value := quoted.get(key)), str) and value.strip()
                    ),
                    None,
                )
                if quoted_text:
                    content = f"{content}\n[הודעה מצוטטת לצורך הקשר בלבד: {quoted_text}]"
            if not content:
                continue
            if event.direction is EventDirection.INBOUND:
                force_confirmation = True
                label = event.actor_display_name or "אדם אחר"
                policy_note = "בקשה נכנסת: להציע כמשימה בלבד, לא לקבל אוטומטית."
            else:
                label = "אני"
                policy_note = "הודעה שנכתבה על ידי המשתמש."
            local_time = event.occurred_at.astimezone(self._timezone).strftime("%d.%m %H:%M")
            lines.append(f"[{local_time}] {label}: {content}\n[{policy_note}]")
        if not lines:
            return IntakeResult(event_id=str(events[-1].id), created=False)

        last_event = events[-1]
        normalized = NormalizedEvent(
            source=EventSource.WHATSAPP,
            source_account=session_id,
            external_id=f"batch:{batch_key}",
            event_type="conversation.urgent" if urgent else "conversation.batch",
            direction=(
                EventDirection.INBOUND
                if any(event.direction is EventDirection.INBOUND for event in events)
                else EventDirection.OUTBOUND
            ),
            occurred_at=last_event.occurred_at,
            received_at=require_aware(self._now()),
            actor_external_id=last_event.actor_external_id,
            actor_display_name=last_event.actor_display_name,
            conversation_external_id=conversation.external_chat_id,
            content_text="\n\n".join(lines),
            payload_json={
                "whatsapp_source_event_ids": [str(event.id) for event in events],
                "force_confirmation": force_confirmation,
                "conversation_type": conversation.chat_type.value,
                "conversation_display_name": (
                    conversation.display_name
                    if conversation.chat_type is WhatsAppConversationType.PRIVATE
                    else None
                ),
                "urgent_bypass": urgent,
                "untrusted_source": True,
            },
            dedupe_key=f"{session_id}:conversation:{batch_key}",
        )
        result = await self._intake.ingest(normalized)
        if result.created:
            await self._record_derived_person_context(conversation, events, result)
        return result

    async def _extract_event_media(self, session_id: str, event: Event) -> str | None:
        media_payload = event.payload_json.get("data", {}).get("media")
        if not isinstance(media_payload, dict) or self._read_client is None:
            return None
        message_id = event.external_id
        downloaded = await self._read_client.download_media(session_id, message_id)
        if downloaded is None:
            return None
        if len(downloaded.content) > self._settings.whatsapp_max_media_bytes:
            return None
        try:
            return await self._media.extract_text(
                MediaAttachment(
                    content=downloaded.content,
                    mime_type=downloaded.mime_type,
                    filename=downloaded.filename,
                    file_unique_id=message_id,
                )
            )
        except MediaValidationError:
            return None

    async def refresh_archives_if_due(self, at: datetime | None = None) -> int:
        if self._read_client is None or not self._settings.whatsapp_ignore_archived:
            return 0
        effective_at = require_aware(at or self._now())
        async with self._session_factory() as session:
            states = list((await session.scalars(select(WhatsAppSessionState))).all())
        refreshed = 0
        for state in states:
            if (
                state.archive_refreshed_at is not None
                and state.archive_refreshed_at
                + timedelta(minutes=self._settings.whatsapp_archive_refresh_minutes)
                > effective_at
            ):
                continue
            try:
                archived_ids = await self._read_client.archived_chat_ids(state.session_id)
            except Exception:
                async with self._session_factory() as session:
                    stored = await session.get(WhatsAppSessionState, state.session_id)
                    if stored is not None:
                        stored.archive_state_reliable = False
                        stored.archive_refreshed_at = effective_at
                        await session.commit()
                continue
            async with self._session_factory() as session:
                conversations = list(
                    (
                        await session.scalars(
                            select(WhatsAppConversation).where(
                                WhatsAppConversation.session_id == state.session_id
                            )
                        )
                    ).all()
                )
                for conversation in conversations:
                    conversation.archived = conversation.external_chat_id in archived_ids
                stored = await session.get(WhatsAppSessionState, state.session_id)
                if stored is not None:
                    stored.archive_state_reliable = True
                    stored.archive_refreshed_at = effective_at
                await session.commit()
            refreshed += 1
        return refreshed

    async def run_initial_history_review(self, at: datetime | None = None) -> int:
        if self._read_client is None or not self._settings.openwa_configured:
            return 0
        assert self._settings.openwa_session_id is not None
        effective_at = require_aware(at or self._now())
        session_id = self._settings.openwa_session_id
        async with self._session_factory() as session:
            state = await self._get_or_create_session(session, session_id)
            if state.initial_review_status is WhatsAppInitialReviewStatus.COMPLETED:
                return 0
            previous_status = state.initial_review_status
            if (
                previous_status is WhatsAppInitialReviewStatus.FAILED
                and self._settings.whatsapp_ignore_archived
                and state.archive_refreshed_at is not None
                and state.archive_refreshed_at
                + timedelta(minutes=self._settings.whatsapp_archive_refresh_minutes)
                > effective_at
            ):
                return 0
            state.initial_review_status = WhatsAppInitialReviewStatus.RUNNING
            await session.commit()

        await self.refresh_archives_if_due(effective_at)
        try:
            archived_ids = (
                await self._read_client.archived_chat_ids(session_id)
                if self._settings.whatsapp_ignore_archived
                else set()
            )
        except Exception:
            async with self._session_factory() as session:
                state = await self._get_or_create_session(session, session_id)
                state.initial_review_status = WhatsAppInitialReviewStatus.FAILED
                state.archive_state_reliable = False
                await session.commit()
            if previous_status is not WhatsAppInitialReviewStatus.FAILED:
                await self._notifier.send_text(
                    "⚠️ סקירת WhatsApp הראשונית הושהתה: גרסת OpenWA המותקנת אינה "
                    "מספקת מצב ארכיון אמין. יש לאמת את ה־Swagger או להגדיר רשימת התעלמות."
                )
            return 0
        since = effective_at - timedelta(days=self._settings.whatsapp_initial_history_days)
        rows = await self._read_client.history(
            session_id,
            since,
            self._settings.whatsapp_history_max_messages,
            self._settings.whatsapp_history_max_messages_per_chat,
        )
        findings = 0
        latest = since
        for history_message in rows:
            data = history_message.data
            if data.timestamp is None or data.chat_id is None or data.stable_id is None:
                continue
            latest = max(latest, data.timestamp)
            if (
                data.chat_id in self._settings.whatsapp_ignored_chat_ids
                or data.chat_id in archived_ids
                or data.is_archived is True
            ):
                continue
            async with self._session_factory() as session:
                conversation = await self._find_conversation(session, session_id, data.chat_id)
                if conversation is not None and (conversation.archived or conversation.ignored):
                    continue
            if self._local_relevance_reason(data) is not None:
                continue
            result = await self._intake.ingest(
                NormalizedEvent(
                    source=EventSource.WHATSAPP,
                    source_account=session_id,
                    external_id=data.stable_id,
                    event_type="history.review",
                    direction=(EventDirection.OUTBOUND if data.from_me else EventDirection.INBOUND),
                    occurred_at=data.timestamp,
                    received_at=effective_at,
                    actor_external_id=data.sender_id,
                    actor_display_name=data.sender_name,
                    conversation_external_id=data.chat_id,
                    content_text=data.content,
                    payload_json={
                        "force_confirmation": True,
                        "review_only": True,
                        "history_window_days": self._settings.whatsapp_initial_history_days,
                    },
                    dedupe_key=f"{session_id}:history:{data.stable_id}",
                )
            )
            if not result.approval_ids:
                continue
            async with self._session_factory() as session:
                for approval_id in result.approval_ids:
                    approval = await session.get(ApprovalRequest, uuid.UUID(approval_id))
                    if approval is None:
                        continue
                    item = approval.action_payload["item"]
                    session.add(
                        WhatsAppHistoricalFinding(
                            session_id=session_id,
                            conversation_id=None,
                            source_event_ids=[result.event_id],
                            interpretation_payload=item,
                            status=HistoricalFindingStatus.PENDING,
                            confidence=float(item["confidence"]),
                            dedupe_key=f"history:{approval.dedupe_key}",
                        )
                    )
                    findings += 1
                await session.commit()

        async with self._session_factory() as session:
            state = await self._get_or_create_session(session, session_id)
            state.initial_review_status = WhatsAppInitialReviewStatus.COMPLETED
            state.history_watermark = latest
            await session.commit()
        if findings:
            await self._notifier.send_text(
                "📚 סקירת WhatsApp — השבוע האחרון\n"
                "━━━━━━━━━━━━\n"
                f"מצאתי {findings} פריטים אפשריים. כל אחד נשמר לבדיקתך ודורש אישור מפורש."
            )
        return findings

    async def redact_expired_events(self, at: datetime | None = None) -> int:
        effective_at = require_aware(at or self._now())
        cutoff = effective_at - timedelta(days=self._settings.whatsapp_event_retention_days)
        async with self._session_factory() as session:
            events = list(
                (
                    await session.scalars(
                        select(Event).where(
                            Event.source == EventSource.WHATSAPP,
                            Event.occurred_at < cutoff,
                            Event.redacted_at.is_(None),
                        )
                    )
                ).all()
            )
            for event in events:
                event.content_text = None
                event.payload_json = {
                    "redacted": True,
                    "original_event_type": event.event_type,
                }
                event.redacted_at = effective_at
            if events:
                session.add(
                    AuditLog(
                        actor="whatsapp_service",
                        action="redact_expired_whatsapp_content",
                        target="whatsapp_events",
                        result="redacted",
                        redacted_metadata={"count": len(events)},
                    )
                )
            await session.commit()
        return len(events)

    async def poll_connectivity(self, at: datetime | None = None) -> int:
        if self._read_client is None or not self._settings.openwa_configured:
            return 0
        assert self._settings.openwa_session_id is not None
        effective_at = require_aware(at or self._now())
        snapshot = await self._read_client.connection_status(self._settings.openwa_session_id)
        async with self._session_factory() as session:
            state = await self._get_or_create_session(session, snapshot.session_id)
            state.api_reachable = snapshot.reachable
            await session.commit()
        status = self._normalize_session_status(snapshot.status)
        return int(await self._transition_session(snapshot.session_id, status, effective_at))

    async def send_disconnect_warning_if_due(self, at: datetime | None = None) -> int:
        effective_at = require_aware(at or self._now())
        cutoff = effective_at - timedelta(
            minutes=self._settings.whatsapp_disconnect_warning_minutes
        )
        async with self._session_factory() as session:
            states = list(
                (
                    await session.scalars(
                        select(WhatsAppSessionState).where(
                            WhatsAppSessionState.status == WhatsAppSessionStatus.DISCONNECTED,
                            WhatsAppSessionState.disconnected_at <= cutoff,
                            WhatsAppSessionState.disconnect_warning_sent_at.is_(None),
                        )
                    )
                ).all()
            )
            for state in states:
                state.disconnect_warning_sent_at = effective_at
                state.relink_required = True
            await session.commit()
        for _state in states:
            await self._notifier.send_text(
                "🚨 WhatsApp עדיין מנותק כבר יותר מ־10 דקות. ייתכן שצריך לקשר מחדש את המכשיר."
            )
        return len(states)

    async def _record_session_webhook(self, webhook: OpenWAWebhook, received_at: datetime) -> None:
        async with self._session_factory() as session:
            state = await self._get_or_create_session(session, webhook.session_id)
            state.api_reachable = True
            state.last_webhook_at = received_at
            await session.commit()

    async def _apply_session_event(self, webhook: OpenWAWebhook, at: datetime) -> None:
        raw_status = webhook.data.status or webhook.event
        status = self._normalize_session_status(raw_status)
        await self._transition_session(webhook.session_id, status, at)

    async def _transition_session(
        self,
        session_id: str,
        status: WhatsAppSessionStatus,
        at: datetime,
    ) -> bool:
        notify: str | None = None
        async with self._session_factory() as session:
            state = await self._get_or_create_session(session, session_id)
            previous = state.status
            if previous is status:
                return False
            state.status = status
            if status is WhatsAppSessionStatus.CONNECTED:
                state.connected_at = at
                state.relink_required = False
                state.disconnect_warning_sent_at = None
                if previous in {
                    WhatsAppSessionStatus.DISCONNECTED,
                    WhatsAppSessionStatus.RECONNECTING,
                    WhatsAppSessionStatus.FAILED,
                }:
                    notify = "✅ החיבור ל־WhatsApp חזר."
                state.incident_id = None
            elif status is WhatsAppSessionStatus.DISCONNECTED:
                state.disconnected_at = at
                state.incident_id = state.incident_id or uuid.uuid4().hex
                state.disconnect_warning_sent_at = None
                notify = "⚠️ החיבור ל־WhatsApp נותק. OpenWA ינסה להתחבר מחדש."
            elif status is WhatsAppSessionStatus.QR_REQUIRED:
                state.relink_required = True
            await session.commit()
        if notify is not None:
            await self._notifier.send_text(notify)
        return True

    async def _store_call_event(
        self, webhook: OpenWAWebhook, received_at: datetime
    ) -> OpenWAWebhookResponse:
        data = webhook.data
        normalized = NormalizedEvent(
            source=EventSource.WHATSAPP,
            source_account=webhook.session_id,
            external_id=webhook.stable_event_id(received_at),
            event_type=webhook.event,
            direction=EventDirection.INBOUND,
            occurred_at=webhook.event_time(received_at),
            received_at=received_at,
            actor_external_id=data.sender_id,
            actor_display_name=data.sender_name,
            conversation_external_id=data.chat_id,
            content_text="שיחת WhatsApp נכנסת",
            payload_json=webhook.model_dump(mode="json", by_alias=True),
            dedupe_key=(
                f"{webhook.session_id}:{webhook.event}:{webhook.stable_event_id(received_at)}"
            ),
        )
        async with self._session_factory() as session:
            event, created = await EventRepository(session).add_if_absent(normalized)
            event.processing_status = ProcessingStatus.PROCESSED
            await session.commit()
        if created:
            await self._notifier.send_text(
                f"📞 שיחת WhatsApp נכנסת מ־{data.sender_name or 'איש קשר לא מזוהה'}."
            )
        return OpenWAWebhookResponse(
            accepted=True,
            duplicate=not created,
            event_id=str(event.id),
        )

    async def _store_message_event(
        self,
        session: AsyncSession,
        webhook: OpenWAWebhook,
        received_at: datetime,
    ) -> tuple[Event, bool]:
        data = webhook.data
        event_id = webhook.stable_event_id(received_at)
        normalized = NormalizedEvent(
            source=EventSource.WHATSAPP,
            source_account=webhook.session_id,
            external_id=event_id,
            event_type=webhook.event,
            direction=(
                EventDirection.OUTBOUND
                if data.from_me or webhook.event == "message.sent"
                else EventDirection.INBOUND
            ),
            occurred_at=webhook.event_time(received_at),
            received_at=received_at,
            actor_external_id=data.sender_id,
            actor_display_name=data.sender_name,
            conversation_external_id=data.chat_id,
            content_text=data.content,
            payload_json=webhook.model_dump(mode="json", by_alias=True),
            dedupe_key=f"{webhook.session_id}:{webhook.event}:{event_id}",
        )
        return await EventRepository(session).add_if_absent(normalized)

    async def _upsert_conversation(
        self,
        session: AsyncSession,
        webhook: OpenWAWebhook,
        received_at: datetime,
    ) -> WhatsAppConversation:
        data = webhook.data
        assert data.chat_id is not None
        conversation = await self._find_conversation(session, webhook.session_id, data.chat_id)
        if conversation is None:
            conversation = WhatsAppConversation(
                session_id=webhook.session_id,
                external_chat_id=data.chat_id,
                chat_type=(
                    WhatsAppConversationType.GROUP
                    if data.is_group
                    else WhatsAppConversationType.PRIVATE
                ),
                display_name=data.chat_name,
                archived=bool(data.is_archived),
                ignored=data.chat_id in self._settings.whatsapp_ignored_chat_ids,
                last_message_at=webhook.event_time(received_at),
            )
            session.add(conversation)
            await session.flush()
        else:
            conversation.display_name = data.chat_name or conversation.display_name
            if data.is_archived is not None:
                conversation.archived = data.is_archived
            conversation.ignored = (
                conversation.ignored or data.chat_id in self._settings.whatsapp_ignored_chat_ids
            )
            conversation.last_message_at = webhook.event_time(received_at)
        return conversation

    async def _buffer_event(
        self,
        session: AsyncSession,
        conversation: WhatsAppConversation,
        event: Event,
        received_at: datetime,
        *,
        urgent: bool = False,
    ) -> WhatsAppConversationBuffer:
        buffer = (
            await session.scalars(
                select(WhatsAppConversationBuffer).where(
                    WhatsAppConversationBuffer.conversation_id == conversation.id,
                    WhatsAppConversationBuffer.status == WhatsAppBufferStatus.PENDING,
                )
            )
        ).first()
        flush_at = (
            received_at
            if urgent
            else received_at + timedelta(seconds=self._settings.whatsapp_conversation_idle_seconds)
        )
        if buffer is None:
            buffer = WhatsAppConversationBuffer(
                conversation_id=conversation.id,
                first_message_at=event.occurred_at,
                last_message_at=event.occurred_at,
                flush_at=flush_at,
                status=WhatsAppBufferStatus.PENDING,
                urgent=urgent,
                event_ids=[str(event.id)],
                dedupe_key=f"{conversation.session_id}:{conversation.external_chat_id}:{uuid.uuid4().hex}",
            )
            session.add(buffer)
            await session.flush()
        else:
            event_ids = list(buffer.event_ids)
            if str(event.id) not in event_ids:
                event_ids.append(str(event.id))
            buffer.event_ids = event_ids
            buffer.last_message_at = event.occurred_at
            buffer.urgent = buffer.urgent or urgent
            buffer.flush_at = min(buffer.flush_at, flush_at) if buffer.urgent else flush_at
        return buffer

    async def _upsert_person(
        self,
        session: AsyncSession,
        event: Event,
        conversation: WhatsAppConversation,
        data: OpenWAEventData,
    ) -> None:
        external_id = (
            data.sender_id
            if event.direction is EventDirection.INBOUND
            else conversation.external_chat_id
            if conversation.chat_type is WhatsAppConversationType.PRIVATE
            else None
        )
        if external_id is None:
            return
        display_name = (
            data.sender_name
            if event.direction is EventDirection.INBOUND
            else conversation.display_name
        ) or external_id
        person = (
            await session.scalars(
                select(Person).where(
                    Person.channel == "whatsapp",
                    Person.external_id == external_id,
                )
            )
        ).first()
        if person is None:
            person = Person(
                channel="whatsapp",
                external_id=external_id,
                display_name=display_name,
                aliases=[],
                conversation_ids=[conversation.external_chat_id],
                operational_facts={
                    (
                        "incoming_relevant_messages"
                        if event.direction is EventDirection.INBOUND
                        else "outgoing_relevant_messages"
                    ): 1
                },
                source_event_ids=[str(event.id)],
                confidence=1.0,
                last_relevant_interaction_at=event.occurred_at,
            )
            session.add(person)
            return
        if display_name != person.display_name:
            aliases = list(person.aliases)
            if person.display_name not in aliases:
                aliases.append(person.display_name)
            person.aliases = aliases[-20:]
            person.display_name = display_name
        conversations = list(person.conversation_ids)
        if conversation.external_chat_id not in conversations:
            conversations.append(conversation.external_chat_id)
        person.conversation_ids = conversations[-100:]
        source_ids = list(person.source_event_ids)
        source_ids.append(str(event.id))
        person.source_event_ids = source_ids[-100:]
        facts = dict(person.operational_facts)
        message_counter = (
            "incoming_relevant_messages"
            if event.direction is EventDirection.INBOUND
            else "outgoing_relevant_messages"
        )
        facts[message_counter] = int(facts.get(message_counter, 0)) + 1
        person.operational_facts = facts
        person.last_relevant_interaction_at = event.occurred_at

    async def _record_derived_person_context(
        self,
        conversation: WhatsAppConversation,
        events: Sequence[Event],
        result: IntakeResult,
    ) -> None:
        identities: set[str] = set()
        for event in events:
            if event.direction is EventDirection.INBOUND and event.actor_external_id:
                identities.add(event.actor_external_id)
            elif conversation.chat_type is WhatsAppConversationType.PRIVATE:
                identities.add(conversation.external_chat_id)
        if not identities:
            return
        async with self._session_factory() as session:
            people = list(
                (
                    await session.scalars(
                        select(Person).where(
                            Person.channel == "whatsapp",
                            Person.external_id.in_(identities),
                        )
                    )
                ).all()
            )
            for person in people:
                facts = dict(person.operational_facts)
                if any(event.direction is EventDirection.INBOUND for event in events):
                    facts["requests_to_user"] = int(facts.get("requests_to_user", 0)) + len(
                        result.approval_ids
                    )
                facts["derived_commitments"] = int(facts.get("derived_commitments", 0)) + len(
                    result.commitment_ids
                )
                facts["derived_tasks"] = int(facts.get("derived_tasks", 0)) + len(result.task_ids)
                facts["open_follow_ups"] = int(facts.get("open_follow_ups", 0)) + len(
                    result.approval_ids
                )
                person.operational_facts = facts
            await session.commit()

    async def _handle_edit_or_revocation(
        self,
        session: AsyncSession,
        event: Event,
        webhook: OpenWAWebhook,
        at: datetime,
    ) -> bool:
        data = webhook.data
        original_id = data.original_message_id or data.message_id or data.id
        if original_id is None:
            return False
        original = (
            await session.scalars(
                select(Event).where(
                    Event.source == EventSource.WHATSAPP,
                    Event.source_account == webhook.session_id,
                    Event.external_id == original_id,
                    Event.event_type.in_(["message.received", "message.sent"]),
                )
            )
        ).first()
        if original is None:
            return False
        event.supersedes_event_id = original.id
        if webhook.event == "message.revoked":
            original.revoked_at = at
        buffers = list(
            (
                await session.scalars(
                    select(WhatsAppConversationBuffer).where(
                        WhatsAppConversationBuffer.status == WhatsAppBufferStatus.PENDING
                    )
                )
            ).all()
        )
        for buffer in buffers:
            if str(original.id) not in buffer.event_ids:
                continue
            buffer.event_ids = [value for value in buffer.event_ids if value != str(original.id)]
            if not buffer.event_ids:
                buffer.status = WhatsAppBufferStatus.CANCELLED

        pending, executed = await self._interpretation_state(session, original.id)
        for approval in pending:
            approval.status = ApprovalStatus.REJECTED
            approval.resolved_at = at
            commitment_id = approval.action_payload.get("commitment_id")
            task_id = approval.action_payload.get("task_id")
            if commitment_id:
                commitment = await session.get(Commitment, uuid.UUID(str(commitment_id)))
                if commitment is not None:
                    commitment.status = CommitmentStatus.CANCELLED
            if task_id:
                task = await session.get(Task, uuid.UUID(str(task_id)))
                if task is not None:
                    task.status = TaskStatus.CANCELLED
        if executed:
            change_kind = "revocation" if webhook.event == "message.revoked" else "edit"
            dedupe_key = f"{change_kind}:{original.id}:{event.id}"
            existing = await session.scalar(
                select(WhatsAppHistoricalFinding).where(
                    WhatsAppHistoricalFinding.dedupe_key == dedupe_key
                )
            )
            if existing is None:
                session.add(
                    WhatsAppHistoricalFinding(
                        session_id=webhook.session_id,
                        conversation_id=None,
                        source_event_ids=[str(original.id), str(event.id)],
                        interpretation_payload={
                            "kind": change_kind,
                            "old_text": original.content_text,
                            "new_text": event.content_text,
                            "requires_user_choice": True,
                        },
                        status=HistoricalFindingStatus.PENDING,
                        confidence=1.0,
                        dedupe_key=dedupe_key,
                    )
                )
            await session.commit()
            await self._notifier.send_text(
                "✏️ הודעת WhatsApp שכבר יצרה פריט נערכה או נמחקה. "
                "הפריט הקיים נשמר ולא שונה ללא אישורך."
            )
            return True
        return False

    async def _interpretation_state(
        self, session: AsyncSession, original_event_id: uuid.UUID
    ) -> tuple[list[ApprovalRequest], bool]:
        batch_events = list(
            (
                await session.scalars(
                    select(Event).where(
                        Event.source == EventSource.WHATSAPP,
                        Event.event_type.in_(["conversation.batch", "conversation.urgent"]),
                    )
                )
            ).all()
        )
        source_ids = [
            event.id
            for event in batch_events
            if str(original_event_id) in event.payload_json.get("whatsapp_source_event_ids", [])
        ]
        if not source_ids:
            return [], False
        approvals = list(
            (
                await session.scalars(
                    select(ApprovalRequest).where(ApprovalRequest.source_event_id.in_(source_ids))
                )
            ).all()
        )
        return (
            [approval for approval in approvals if approval.status is ApprovalStatus.PENDING],
            any(approval.status is ApprovalStatus.EXECUTED for approval in approvals),
        )

    def _ignored_reason(
        self, conversation: WhatsAppConversation, data: OpenWAEventData
    ) -> str | None:
        if conversation.ignored:
            return "manual_denylist"
        if self._settings.whatsapp_ignore_archived and conversation.archived:
            return "archived_chat"
        return self._local_relevance_reason(data)

    def _local_relevance_reason(self, data: OpenWAEventData) -> str | None:
        media_type = (
            (data.media.media_type or data.message_type or "").lower()
            if data.media is not None
            else (data.message_type or "").lower()
        )
        mime_type = data.media.mime_type.lower() if data.media and data.media.mime_type else ""
        if media_type == "video" or mime_type.startswith("video/"):
            return "video_not_supported"
        if (
            data.media is not None
            and data.media.size is not None
            and data.media.size > self._settings.whatsapp_max_media_bytes
        ):
            return "media_too_large"
        if data.media is not None and (
            media_type in SUPPORTED_MEDIA_TYPES
            or mime_type.startswith(("audio/", "image/", "text/"))
            or mime_type
            in {
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
        ):
            return None
        content = (data.content or "").strip()
        if not content:
            return "empty_or_unsupported"
        if IRRELEVANT_TEXT.fullmatch(content):
            return "local_noise_filter"
        if data.is_group and not data.from_me:
            directed = data.user_mentioned or bool(data.mentioned_ids)
            if data.model_extra:
                directed = directed or bool(data.model_extra.get("directedToUser"))
            if not directed:
                return "unaddressed_group_message"
        if not RELEVANT_TEXT.search(content) and not RELATIVE_MINUTES_TEXT.search(content):
            return "not_locally_relevant"
        return None

    def _is_urgent_user_commitment(self, data: OpenWAEventData, occurred_at: datetime) -> bool:
        content = data.content or ""
        if not data.from_me or not USER_COMMITMENT_TEXT.search(content):
            return False
        relative = RELATIVE_MINUTES_TEXT.search(content)
        if relative:
            return int(relative.group(1)) <= self._settings.whatsapp_urgent_bypass_minutes
        explicit = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", content)
        if explicit is None:
            return False
        local_occurred = occurred_at.astimezone(self._timezone)
        due = local_occurred.replace(
            hour=int(explicit.group(1)),
            minute=int(explicit.group(2)),
            second=0,
            microsecond=0,
        )
        delta = due - local_occurred
        return (
            timedelta(0) < delta <= timedelta(minutes=self._settings.whatsapp_urgent_bypass_minutes)
        )

    @staticmethod
    def _normalize_session_status(value: str) -> WhatsAppSessionStatus:
        normalized = value.casefold().replace("session.", "")
        if normalized in {"connected", "ready", "authenticated"}:
            return WhatsAppSessionStatus.CONNECTED
        if normalized in {"qr", "qr_ready", "qr_required", "scan_qr", "action_required"}:
            return WhatsAppSessionStatus.QR_REQUIRED
        if normalized in {"disconnected", "offline"}:
            return WhatsAppSessionStatus.DISCONNECTED
        if normalized in {"created", "initializing", "reconnecting", "connecting"}:
            return WhatsAppSessionStatus.RECONNECTING
        if normalized in {"authenticating"}:
            return WhatsAppSessionStatus.AUTHENTICATING
        if normalized in {"failed", "error"}:
            return WhatsAppSessionStatus.FAILED
        return WhatsAppSessionStatus.UNKNOWN

    async def _get_or_create_session(
        self, session: AsyncSession, session_id: str
    ) -> WhatsAppSessionState:
        state = await session.get(WhatsAppSessionState, session_id)
        if state is None:
            state = WhatsAppSessionState(session_id=session_id)
            session.add(state)
            await session.flush()
        return state

    @staticmethod
    async def _find_conversation(
        session: AsyncSession, session_id: str, chat_id: str
    ) -> WhatsAppConversation | None:
        return (
            await session.scalars(
                select(WhatsAppConversation).where(
                    WhatsAppConversation.session_id == session_id,
                    WhatsAppConversation.external_chat_id == chat_id,
                )
            )
        ).first()
