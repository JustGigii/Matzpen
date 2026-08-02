from datetime import datetime

from personal_agent.integrations.google_calendar.base import CalendarEvent


class FakeCalendarProvider:
    def __init__(self, events: list[CalendarEvent] | None = None) -> None:
        self.events = list(events or [])

    async def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return [event for event in self.events if event.start < end and event.end > start]

    async def create_event(self, event: CalendarEvent) -> CalendarEvent:
        existing = next(
            (item for item in self.events if item.external_id == event.external_id), None
        )
        if existing is not None:
            return existing
        self.events.append(event)
        return event
