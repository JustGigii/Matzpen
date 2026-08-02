import hmac
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from personal_agent.api.dependencies import get_app_settings
from personal_agent.core.config import Settings
from personal_agent.domain.schemas import MorningBriefResponse, MorningBriefTriggerRequest
from personal_agent.services.briefs import MorningBriefService

router = APIRouter(prefix="/api/briefs", tags=["briefs"])


@router.post("/morning/trigger", response_model=MorningBriefResponse)
async def trigger_morning_brief(
    payload: MorningBriefTriggerRequest,
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> MorningBriefResponse:
    configured_token = settings.shortcut_bearer_token
    if configured_token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shortcut integration is not configured",
        )
    expected = f"Bearer {configured_token.get_secret_value()}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    service = cast(MorningBriefService, request.app.state.morning_brief_service)
    return await service.trigger(payload.source, force=payload.force)
