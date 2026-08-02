from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.domain.enums import ProcessingStatus
from personal_agent.domain.models import Event
from personal_agent.domain.schemas import NormalizedEvent


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, candidate: NormalizedEvent) -> tuple[Event, bool]:
        existing = await self.find_by_dedupe(
            candidate.source.value, candidate.source_account, candidate.dedupe_key or ""
        )
        if existing is not None:
            return existing, False

        event = Event(
            source=candidate.source,
            source_account=candidate.source_account,
            external_id=candidate.external_id,
            event_type=candidate.event_type,
            direction=candidate.direction,
            occurred_at=candidate.occurred_at,
            received_at=candidate.received_at,
            actor_external_id=candidate.actor_external_id,
            actor_display_name=candidate.actor_display_name,
            conversation_external_id=candidate.conversation_external_id,
            content_text=candidate.content_text,
            payload_json=candidate.payload_json,
            dedupe_key=candidate.dedupe_key or "",
            sensitivity=candidate.sensitivity,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(event)
                await self._session.flush()
        except IntegrityError:
            existing = await self.find_by_dedupe(
                candidate.source.value, candidate.source_account, candidate.dedupe_key or ""
            )
            if existing is None:
                raise
            return existing, False
        return event, True

    async def find_by_dedupe(
        self, source: str, source_account: str, dedupe_key: str
    ) -> Event | None:
        statement = select(Event).where(
            Event.source == source,
            Event.source_account == source_account,
            Event.dedupe_key == dedupe_key,
        )
        return (await self._session.scalars(statement)).first()

    async def set_processing_status(self, event: Event, status: ProcessingStatus) -> None:
        event.processing_status = status
        await self._session.flush()
