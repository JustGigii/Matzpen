from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_agent.core.time import require_aware


class CalendarEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str
    summary: str
    start: datetime
    end: datetime
    description: str | None = None
    recurrence: list[str] = Field(default_factory=list)

    @field_validator("start", "end")
    @classmethod
    def datetimes_must_be_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class CalendarProvider(Protocol):
    async def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...

    async def create_event(self, event: CalendarEvent) -> CalendarEvent: ...
