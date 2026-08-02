import hashlib
import hmac
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_agent.api.dependencies import get_app_settings, get_intake_service
from personal_agent.core.config import Settings
from personal_agent.core.time import require_aware
from personal_agent.domain.enums import EventDirection, EventSource
from personal_agent.domain.schemas import IntakeResult, NormalizedEvent
from personal_agent.services.intake import IntakeService

router = APIRouter(prefix="/api/intake", tags=["intake"])


class ShortcutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    content: str = Field(min_length=1, max_length=20_000)
    captured_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


@router.post("/shortcut", response_model=IntakeResult)
async def receive_shortcut(
    payload: ShortcutPayload,
    request: Request,
    intake_service: Annotated[IntakeService, Depends(get_intake_service)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> IntakeResult:
    configured_token = settings.shortcut_bearer_token
    if configured_token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shortcut intake is not configured",
        )
    expected = f"Bearer {configured_token.get_secret_value()}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    material = json.dumps(
        {
            "captured_at": payload.captured_at.isoformat(),
            "content": payload.content,
            "metadata": payload.metadata,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    external_id = hashlib.sha256(material).hexdigest()
    return await intake_service.ingest(
        NormalizedEvent(
            source=EventSource.SHORTCUT,
            source_account="iphone-shortcut",
            external_id=external_id,
            event_type="text.captured",
            direction=EventDirection.INTERNAL,
            occurred_at=payload.captured_at,
            received_at=request.app.state.clock(),
            actor_external_id="self",
            actor_display_name="User",
            content_text=payload.content,
            payload_json=payload.model_dump(mode="json"),
            dedupe_key=external_id,
        )
    )
