from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_agent.core.time import require_aware

OpenWAEventName = str


class OpenWAMedia(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    mime_type: str | None = Field(default=None, alias="mimeType")
    filename: str | None = Field(default=None, alias="fileName")
    size: int | None = Field(default=None, ge=0)
    media_type: str | None = Field(default=None, alias="type")


class OpenWAEventData(BaseModel):
    """Version-tolerant read model; installed OpenAPI remains the deployment authority."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | None = None
    message_id: str | None = Field(default=None, alias="messageId")
    original_message_id: str | None = Field(default=None, alias="originalMessageId")
    chat_id: str | None = Field(default=None, alias="chatId")
    chat_name: str | None = Field(default=None, alias="chatName")
    sender_id: str | None = Field(default=None, alias="senderId")
    sender_name: str | None = Field(default=None, alias="senderName")
    timestamp: datetime | None = None
    body: str | None = None
    text: str | None = None
    caption: str | None = None
    from_me: bool = Field(default=False, alias="fromMe")
    is_group: bool = Field(default=False, alias="isGroup")
    is_archived: bool | None = Field(default=None, alias="isArchived")
    user_mentioned: bool = Field(default=False, alias="userMentioned")
    mentioned_ids: list[str] = Field(default_factory=list, alias="mentionedIds")
    quoted_message: dict[str, Any] | None = Field(default=None, alias="quotedMessage")
    message_type: str | None = Field(default=None, alias="messageType")
    media: OpenWAMedia | None = None
    status: str | None = None
    reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_common_openwa_variants(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "messageId" not in normalized:
            normalized["messageId"] = normalized.get("waMessageId") or normalized.get("message_id")
        if "chatId" not in normalized:
            normalized["chatId"] = (
                normalized.get("groupId")
                or normalized.get("from")
                or normalized.get("to")
                or (
                    normalized.get("id")
                    if str(normalized.get("id", "")).endswith("@g.us")
                    else None
                )
            )
        if "chatName" not in normalized:
            normalized["chatName"] = (
                normalized.get("subject") or normalized.get("groupName") or normalized.get("name")
            )
        if "senderId" not in normalized:
            normalized["senderId"] = normalized.get("author") or normalized.get("participant")
        if "fromMe" not in normalized and "direction" in normalized:
            normalized["fromMe"] = str(normalized["direction"]).lower() in {
                "outbound",
                "outgoing",
            }
        if "isGroup" not in normalized and normalized.get("chatId"):
            normalized["isGroup"] = str(normalized["chatId"]).endswith("@g.us")
        if "body" not in normalized:
            normalized["body"] = normalized.get("content")
        if "messageType" not in normalized:
            normalized["messageType"] = normalized.get("type")
        media = normalized.get("media")
        if not isinstance(media, dict):
            metadata = normalized.get("metadata")
            media = metadata.get("media") if isinstance(metadata, dict) else None
        if isinstance(media, dict):
            normalized_media = dict(media)
            normalized_media.setdefault("mimeType", normalized_media.get("mimetype"))
            normalized_media.setdefault("fileName", normalized_media.get("filename"))
            normalized_media.setdefault("size", normalized_media.get("sizeBytes"))
            normalized["media"] = normalized_media
        return normalized

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, value: Any) -> Any:
        if isinstance(value, int | float):
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=UTC)
        return value

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware(value)

    @property
    def stable_id(self) -> str | None:
        return self.message_id or self.id

    @property
    def content(self) -> str | None:
        values = [self.body, self.text, self.caption]
        return "\n".join(value.strip() for value in values if value and value.strip()) or None


class OpenWAWebhook(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    event: OpenWAEventName = Field(min_length=1, max_length=255)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=255)
    data: OpenWAEventData
    delivery_id: str | None = Field(default=None, alias="deliveryId")
    idempotency_key: str | None = Field(default=None, alias="idempotencyKey")
    timestamp: datetime | None = None

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, value: Any) -> Any:
        return OpenWAEventData.parse_timestamp(value)

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware(value)

    def event_time(self, received_at: datetime) -> datetime:
        return self.data.timestamp or self.timestamp or require_aware(received_at)

    def stable_event_id(self, received_at: datetime) -> str:
        identifier = self.data.stable_id or self.idempotency_key or self.delivery_id
        if identifier:
            return identifier
        status = self.data.status or self.event
        return f"{status}:{int(self.event_time(received_at).timestamp())}"


class OpenWAWebhookResponse(BaseModel):
    accepted: bool
    duplicate: bool = False
    buffered: bool = False
    ignored_reason: str | None = None
    event_id: str | None = None
    buffer_id: str | None = None


class OpenWAHistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    data: OpenWAEventData


class OpenWAConnectionSnapshot(BaseModel):
    session_id: str
    status: str
    reachable: bool
