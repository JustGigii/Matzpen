from collections.abc import Sequence
from datetime import datetime

from personal_agent.integrations.openwa.client import OpenWAMediaContent
from personal_agent.integrations.openwa.schemas import (
    OpenWAConnectionSnapshot,
    OpenWAHistoryMessage,
)


class FakeOpenWAReadClient:
    """Queued read-only OpenWA data for automated tests."""

    def __init__(
        self,
        *,
        status: str = "connected",
        archived_chat_ids: set[str] | None = None,
        chat_display_names: dict[str, str] | None = None,
        history: Sequence[OpenWAHistoryMessage] = (),
        media: dict[str, OpenWAMediaContent] | None = None,
    ) -> None:
        self.status = status
        self.archived = set(archived_chat_ids or set())
        self.chat_display_names = dict(chat_display_names or {})
        self.history_rows = list(history)
        self.media = dict(media or {})
        self.history_requests: list[tuple[str, datetime, int, int]] = []
        self.media_requests: list[tuple[str, str]] = []
        self.closed = False

    async def connection_status(self, session_id: str) -> OpenWAConnectionSnapshot:
        return OpenWAConnectionSnapshot(
            session_id=session_id,
            status=self.status,
            reachable=self.status != "unreachable",
        )

    async def archived_chat_ids(self, session_id: str) -> set[str]:
        del session_id
        return set(self.archived)

    async def conversation_display_name(self, session_id: str, chat_id: str) -> str | None:
        del session_id
        return self.chat_display_names.get(chat_id)

    async def history(
        self,
        session_id: str,
        since: datetime,
        max_messages: int,
        max_messages_per_chat: int,
    ) -> Sequence[OpenWAHistoryMessage]:
        self.history_requests.append((session_id, since, max_messages, max_messages_per_chat))
        per_chat: dict[str, int] = {}
        result: list[OpenWAHistoryMessage] = []
        for row in self.history_rows:
            chat_id = row.data.chat_id or "unknown"
            if row.data.timestamp is not None and row.data.timestamp < since:
                continue
            if per_chat.get(chat_id, 0) >= max_messages_per_chat:
                continue
            per_chat[chat_id] = per_chat.get(chat_id, 0) + 1
            result.append(row)
            if len(result) >= max_messages:
                break
        return result

    async def download_media(self, session_id: str, message_id: str) -> OpenWAMediaContent | None:
        self.media_requests.append((session_id, message_id))
        return self.media.get(message_id)

    async def aclose(self) -> None:
        self.closed = True
