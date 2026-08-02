import asyncio
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from personal_agent.core.time import require_aware
from personal_agent.integrations.google_calendar.base import CalendarEvent

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


class GoogleCalendarProvider:
    def __init__(
        self,
        token_file: str,
        calendar_id: str,
        timezone: str,
    ) -> None:
        self._token_file = Path(token_file)
        self._calendar_id = calendar_id
        self._timezone = ZoneInfo(timezone)

    async def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return await asyncio.to_thread(self._list_events_sync, start, end)

    async def create_event(self, event: CalendarEvent) -> CalendarEvent:
        return await asyncio.to_thread(self._create_event_sync, event)

    def _service(self) -> Resource:
        if not self._token_file.exists():
            raise RuntimeError(
                f"Google token file does not exist: {self._token_file}. Run the OAuth bootstrap."
            )
        credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
            str(self._token_file), CALENDAR_SCOPES
        )
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self._token_file.write_text(credentials.to_json(), encoding="utf-8")
            self._token_file.chmod(0o600)
        if not credentials.valid:
            raise RuntimeError("Google Calendar OAuth credentials are invalid")
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def _list_events_sync(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        service = self._service()
        response = (
            service.events()
            .list(
                calendarId=self._calendar_id,
                timeMin=require_aware(start).isoformat(),
                timeMax=require_aware(end).isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            )
            .execute()
        )
        return [self._parse_event(item) for item in response.get("items", [])]

    def _create_event_sync(self, event: CalendarEvent) -> CalendarEvent:
        service = self._service()
        body: dict[str, Any] = {
            "id": event.external_id,
            "summary": event.summary,
            "description": event.description,
            "start": {"dateTime": event.start.isoformat(), "timeZone": str(self._timezone)},
            "end": {"dateTime": event.end.isoformat(), "timeZone": str(self._timezone)},
        }
        if event.recurrence:
            body["recurrence"] = event.recurrence
        try:
            created = (
                service.events()
                .insert(calendarId=self._calendar_id, body=body, sendUpdates="none")
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status != 409:
                raise
            created = (
                service.events()
                .get(calendarId=self._calendar_id, eventId=event.external_id)
                .execute()
            )
        return self._parse_event(created)

    def _parse_event(self, item: dict[str, Any]) -> CalendarEvent:
        return CalendarEvent(
            external_id=str(item["id"]),
            summary=str(item.get("summary", "(ללא כותרת)")),
            description=item.get("description"),
            start=self._parse_time(item["start"]),
            end=self._parse_time(item["end"]),
            recurrence=[str(value) for value in item.get("recurrence", [])],
        )

    def _parse_time(self, value: dict[str, Any]) -> datetime:
        if "dateTime" in value:
            return require_aware(datetime.fromisoformat(str(value["dateTime"])))
        local = datetime.combine(date.fromisoformat(str(value["date"])), time.min, self._timezone)
        return local.astimezone(UTC)
