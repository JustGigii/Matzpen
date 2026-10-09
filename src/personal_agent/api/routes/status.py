from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_agent.api.dependencies import get_app_settings
from personal_agent.core.config import Settings
from personal_agent.domain.enums import WhatsAppBufferStatus, WhatsAppSessionStatus
from personal_agent.domain.models import (
    ApprovalRequest,
    CalendarAction,
    Commitment,
    Event,
    MorningBrief,
    Reminder,
    Task,
    WhatsAppConversationBuffer,
    WhatsAppSessionState,
)
from personal_agent.services.control import AgentControl

router = APIRouter(prefix="/api", tags=["status"])


class IntegrationStatus(BaseModel):
    telegram: bool
    gemini: bool
    groq: bool = False
    cerebras: bool = False
    mistral_ocr: bool = False
    oracle: bool = False
    google_calendar: bool
    shortcut: bool
    whatsapp: bool = False


class WhatsAppStatus(BaseModel):
    configured: bool
    reachable: bool
    session_id: str | None = None
    session_state: str = "unknown"
    last_webhook_at: datetime | None = None
    last_processed_event_at: datetime | None = None
    pending_buffers: int = 0
    archive_filter_reliable: bool = False
    initial_history_status: str = "pending"
    disconnected_seconds: int | None = None
    relink_required: bool = False


class StatusResponse(BaseModel):
    state: str
    integrations: IntegrationStatus
    events: int
    tasks: int
    commitments: int
    approvals: int
    reminders: int
    calendar_actions: int
    morning_briefs: int
    whatsapp: WhatsAppStatus


@router.get("/status", response_model=StatusResponse)
async def status_route(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> StatusResponse:
    session_factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    control = cast(AgentControl, request.app.state.control)
    async with session_factory() as session:
        events = await session.scalar(select(func.count()).select_from(Event))
        tasks = await session.scalar(select(func.count()).select_from(Task))
        commitments = await session.scalar(select(func.count()).select_from(Commitment))
        approvals = await session.scalar(select(func.count()).select_from(ApprovalRequest))
        reminders = await session.scalar(select(func.count()).select_from(Reminder))
        calendar_actions = await session.scalar(select(func.count()).select_from(CalendarAction))
        morning_briefs = await session.scalar(select(func.count()).select_from(MorningBrief))
        pending_buffers = await session.scalar(
            select(func.count())
            .select_from(WhatsAppConversationBuffer)
            .where(WhatsAppConversationBuffer.status == WhatsAppBufferStatus.PENDING)
        )
        whatsapp_state = (
            await session.scalars(
                select(WhatsAppSessionState).order_by(WhatsAppSessionState.updated_at.desc())
            )
        ).first()
    now = request.app.state.clock()
    disconnected_seconds = (
        int((now - whatsapp_state.disconnected_at).total_seconds())
        if (
            whatsapp_state is not None
            and whatsapp_state.status is WhatsAppSessionStatus.DISCONNECTED
            and whatsapp_state.disconnected_at is not None
        )
        else None
    )
    return StatusResponse(
        state="paused" if control.paused else "running",
        integrations=IntegrationStatus(
            telegram=settings.telegram_configured,
            gemini=settings.gemini_configured,
            groq=settings.groq_configured,
            cerebras=settings.cerebras_configured,
            mistral_ocr=settings.mistral_configured,
            oracle=settings.oracle_configured,
            google_calendar=settings.google_calendar_configured,
            shortcut=settings.shortcut_configured,
            whatsapp=settings.openwa_configured,
        ),
        events=events or 0,
        tasks=tasks or 0,
        commitments=commitments or 0,
        approvals=approvals or 0,
        reminders=reminders or 0,
        calendar_actions=calendar_actions or 0,
        morning_briefs=morning_briefs or 0,
        whatsapp=WhatsAppStatus(
            configured=settings.openwa_configured,
            reachable=whatsapp_state.api_reachable if whatsapp_state is not None else False,
            session_id=whatsapp_state.session_id if whatsapp_state is not None else None,
            session_state=(
                whatsapp_state.status.value if whatsapp_state is not None else "unknown"
            ),
            last_webhook_at=(
                whatsapp_state.last_webhook_at if whatsapp_state is not None else None
            ),
            last_processed_event_at=(
                whatsapp_state.last_processed_event_at if whatsapp_state is not None else None
            ),
            pending_buffers=pending_buffers or 0,
            archive_filter_reliable=(
                whatsapp_state.archive_state_reliable if whatsapp_state is not None else False
            ),
            initial_history_status=(
                whatsapp_state.initial_review_status.value
                if whatsapp_state is not None
                else "pending"
            ),
            disconnected_seconds=disconnected_seconds,
            relink_required=(
                whatsapp_state.relink_required if whatsapp_state is not None else False
            ),
        ),
    )
