import base64
import binascii
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from urllib.parse import quote, urlparse

import httpx

from personal_agent.integrations.openwa.schemas import (
    OpenWAConnectionSnapshot,
    OpenWAEventData,
    OpenWAHistoryMessage,
)


class OpenWAMediaContent:
    def __init__(self, content: bytes, mime_type: str, filename: str | None = None) -> None:
        self.content = content
        self.mime_type = mime_type
        self.filename = filename


class OpenWAReadCapabilityUnavailable(RuntimeError):
    """The installed OpenWA read API does not expose the requested field."""


class OpenWAReadClient(Protocol):
    """Read-only boundary. Deliberately contains no WhatsApp mutation operation."""

    async def connection_status(self, session_id: str) -> OpenWAConnectionSnapshot: ...

    async def archived_chat_ids(self, session_id: str) -> set[str]: ...

    async def conversation_display_name(self, session_id: str, chat_id: str) -> str | None: ...

    async def history(
        self,
        session_id: str,
        since: datetime,
        max_messages: int,
        max_messages_per_chat: int,
    ) -> Sequence[OpenWAHistoryMessage]: ...

    async def download_media(
        self, session_id: str, message_id: str
    ) -> OpenWAMediaContent | None: ...

    async def aclose(self) -> None: ...


class HttpOpenWAReadClient:
    """GET-only OpenWA adapter; endpoint shapes must be verified against installed Swagger."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        parsed_base_url = urlparse(base_url)
        private_hosts = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}
        private_http = (
            parsed_base_url.scheme == "http" and parsed_base_url.hostname in private_hosts
        )
        remote_https = parsed_base_url.scheme == "https"
        if (
            not (private_http or remote_https)
            or parsed_base_url.username is not None
            or parsed_base_url.password is not None
            or parsed_base_url.query
            or parsed_base_url.fragment
        ):
            raise ValueError("OpenWA must use private HTTP or remote HTTPS")
        if not api_key:
            raise ValueError("OpenWA API key cannot be empty")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"X-API-Key": api_key},
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    async def _get(
        self,
        path: str,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        response = await self._client.get(path.lstrip("/"), params=params)
        response.raise_for_status()
        return response

    async def connection_status(self, session_id: str) -> OpenWAConnectionSnapshot:
        safe_session = quote(session_id, safe="")
        try:
            response = await self._get(f"sessions/{safe_session}")
        except httpx.HTTPError:
            return OpenWAConnectionSnapshot(
                session_id=session_id,
                status="unreachable",
                reachable=False,
            )
        payload = response.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        status = str(data.get("status", "unknown")) if isinstance(data, dict) else "unknown"
        return OpenWAConnectionSnapshot(session_id=session_id, status=status, reachable=True)

    async def archived_chat_ids(self, session_id: str) -> set[str]:
        safe_session = quote(session_id, safe="")
        response = await self._get(
            f"sessions/{safe_session}/chats", params={"limit": 1000, "offset": 0}
        )
        payload = response.json()
        rows = self._rows(payload, "chats")
        if rows and not any("isArchived" in row or "archived" in row for row in rows):
            raise OpenWAReadCapabilityUnavailable(
                "Installed OpenWA chat summaries do not expose archive state"
            )
        return {
            str(row.get("id") or row.get("chatId"))
            for row in rows
            if (row.get("id") or row.get("chatId"))
            and bool(row.get("isArchived", row.get("archived", False)))
        }

    async def conversation_display_name(self, session_id: str, chat_id: str) -> str | None:
        """Resolve a chat label through OpenWA's read-only chat listing when available."""
        safe_session = quote(session_id, safe="")
        try:
            response = await self._get(
                f"sessions/{safe_session}/chats", params={"limit": 1000, "offset": 0}
            )
        except httpx.HTTPError:
            return None
        for row in self._rows(response.json(), "chats"):
            identifier = row.get("id") or row.get("chatId")
            if str(identifier) != chat_id:
                continue
            for key in ("name", "displayName", "pushName", "chatName"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            contact = row.get("contact")
            if isinstance(contact, dict):
                for key in ("name", "displayName", "pushName"):
                    value = contact.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            return None
        return None

    async def history(
        self,
        session_id: str,
        since: datetime,
        max_messages: int,
        max_messages_per_chat: int,
    ) -> Sequence[OpenWAHistoryMessage]:
        safe_session = quote(session_id, safe="")
        response = await self._get(
            f"sessions/{safe_session}/messages",
            params={
                "from": since.isoformat(),
                "limit": max_messages,
                "offset": 0,
            },
        )
        per_chat: dict[str, int] = {}
        result: list[OpenWAHistoryMessage] = []
        for row in self._rows(response.json(), "messages"):
            data = OpenWAEventData.model_validate(row)
            chat_id = data.chat_id or "unknown"
            if data.timestamp is not None and data.timestamp < since:
                continue
            if per_chat.get(chat_id, 0) >= max_messages_per_chat:
                continue
            per_chat[chat_id] = per_chat.get(chat_id, 0) + 1
            result.append(OpenWAHistoryMessage(session_id=session_id, data=data))
            if len(result) >= max_messages:
                break
        return result

    async def download_media(self, session_id: str, message_id: str) -> OpenWAMediaContent | None:
        safe_session = quote(session_id, safe="")
        response = await self._get(
            f"sessions/{safe_session}/messages", params={"limit": 1000, "offset": 0}
        )
        for row in self._rows(response.json(), "messages"):
            identifier = row.get("id") or row.get("messageId")
            if str(identifier) != message_id:
                continue
            media = row.get("media") if isinstance(row.get("media"), dict) else {}
            assert isinstance(media, dict)
            encoded = (
                media.get("data")
                or media.get("base64")
                or row.get("mediaData")
                or row.get("mediaBase64")
            )
            if not isinstance(encoded, str) or not encoded:
                return None
            if encoded.startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                return None
            mime_type = str(
                media.get("mimeType") or row.get("mimeType") or "application/octet-stream"
            )
            filename_value = media.get("fileName") or row.get("fileName")
            filename = str(filename_value) if filename_value else None
            return OpenWAMediaContent(content, mime_type, filename)
        return None

    @staticmethod
    def _rows(payload: object, key: str) -> list[dict[str, object]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []
        candidate = payload.get(key, payload.get("items", payload.get("data", [])))
        if isinstance(candidate, dict):
            candidate = candidate.get(key, candidate.get("items", []))
        return (
            [row for row in candidate if isinstance(row, dict)]
            if isinstance(candidate, list)
            else []
        )

    async def aclose(self) -> None:
        await self._client.aclose()
