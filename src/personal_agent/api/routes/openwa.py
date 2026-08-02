from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError

from personal_agent.api.dependencies import get_app_settings
from personal_agent.core.config import Settings
from personal_agent.integrations.openwa.schemas import OpenWAWebhook, OpenWAWebhookResponse
from personal_agent.integrations.openwa.signature import OpenWASignatureVerifier
from personal_agent.services.whatsapp import WhatsAppService

router = APIRouter(prefix="/api/webhooks", tags=["intake"])


@router.post("/openwa", response_model=OpenWAWebhookResponse, status_code=status.HTTP_202_ACCEPTED)
async def receive_openwa(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> OpenWAWebhookResponse:
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length",
            ) from exc
        if declared_length > settings.openwa_webhook_max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="OpenWA webhook body is too large",
            )

    raw_body = await request.body()
    if len(raw_body) > settings.openwa_webhook_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="OpenWA webhook body is too large",
        )
    signature = request.headers.get(settings.openwa_signature_header)
    verifier = OpenWASignatureVerifier(settings.openwa_webhook_secret.get_secret_value())
    if not verifier.verify(raw_body, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        webhook = OpenWAWebhook.model_validate_json(raw_body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid OpenWA webhook payload",
        ) from exc
    service = cast(WhatsAppService, request.app.state.whatsapp_service)
    return await service.handle_webhook(webhook)
