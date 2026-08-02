from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI

from personal_agent.api.routes.briefs import router as briefs_router
from personal_agent.api.routes.health import router as health_router
from personal_agent.api.routes.openwa import router as openwa_router
from personal_agent.api.routes.shortcuts import router as shortcuts_router
from personal_agent.api.routes.status import router as status_router
from personal_agent.core.config import Settings, get_settings
from personal_agent.core.logging import configure_logging
from personal_agent.core.time import utc_now
from personal_agent.domain.database import Base, create_engine, create_session_factory
from personal_agent.integrations.google_calendar.base import CalendarProvider
from personal_agent.integrations.google_calendar.client import GoogleCalendarProvider
from personal_agent.integrations.llm.base import LLMProvider
from personal_agent.integrations.llm.fake import FakeLLMProvider
from personal_agent.integrations.llm.gemini import GeminiProvider
from personal_agent.integrations.openwa.client import HttpOpenWAReadClient, OpenWAReadClient
from personal_agent.integrations.telegram.base import TelegramNotifier
from personal_agent.integrations.telegram.fake import FakeTelegramNotifier
from personal_agent.integrations.telegram.runtime import TelegramRuntime
from personal_agent.services.briefs import MorningBriefService
from personal_agent.services.calendar import CalendarService
from personal_agent.services.confirmations import ConfirmationService
from personal_agent.services.control import AgentControl
from personal_agent.services.intake import IntakeService
from personal_agent.services.lifecycle import LifecycleService
from personal_agent.services.media import MediaTextService
from personal_agent.services.policy import ApprovalPolicy
from personal_agent.services.reminders import ReminderService
from personal_agent.services.scheduler import SchedulerRuntime
from personal_agent.services.whatsapp import WhatsAppService


def create_app(
    settings: Settings | None = None,
    llm_provider: LLMProvider | None = None,
    notifier: TelegramNotifier | None = None,
    calendar_provider: CalendarProvider | None = None,
    openwa_client: OpenWAReadClient | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    engine = create_engine(resolved_settings.database_url)
    session_factory = create_session_factory(engine)
    control = AgentControl()

    resolved_llm: LLMProvider
    if llm_provider is not None:
        resolved_llm = llm_provider
    elif resolved_settings.gemini_configured:
        assert resolved_settings.gemini_api_key is not None
        assert resolved_settings.gemini_model is not None
        resolved_llm = GeminiProvider(
            resolved_settings.gemini_api_key.get_secret_value(),
            resolved_settings.gemini_model,
            resolved_settings.timezone,
        )
    else:
        resolved_llm = FakeLLMProvider()

    telegram_runtime: TelegramRuntime | None = None
    if notifier is not None:
        resolved_notifier = notifier
    elif resolved_settings.telegram_configured:
        assert resolved_settings.telegram_bot_token is not None
        telegram_runtime = TelegramRuntime(
            resolved_settings.telegram_bot_token.get_secret_value(),
            resolved_settings.telegram_allowed_user_ids,
            session_factory,
            control,
            resolved_settings.timezone,
            resolved_settings.telegram_max_media_bytes,
        )
        resolved_notifier = telegram_runtime
    else:
        resolved_notifier = FakeTelegramNotifier()

    resolved_calendar_provider = calendar_provider
    if resolved_calendar_provider is None and resolved_settings.google_calendar_configured:
        assert resolved_settings.google_token_file is not None
        resolved_calendar_provider = GoogleCalendarProvider(
            resolved_settings.google_token_file,
            resolved_settings.google_calendar_id,
            resolved_settings.timezone,
        )

    calendar_service = (
        CalendarService(
            session_factory,
            resolved_calendar_provider,
            resolved_notifier,
            clock,
            resolved_settings.timezone,
        )
        if resolved_calendar_provider is not None
        else None
    )
    lifecycle_service = LifecycleService(
        session_factory=session_factory,
        notifier=resolved_notifier,
        calendar_provider=resolved_calendar_provider,
        now=clock,
        default_reminder_lead_minutes=resolved_settings.default_reminder_lead_minutes,
    )
    intake_service = IntakeService(
        session_factory=session_factory,
        llm_provider=resolved_llm,
        notifier=resolved_notifier,
        policy=ApprovalPolicy(resolved_settings.internal_action_grace_seconds),
        lifecycle=lifecycle_service,
        now=clock,
        reminder_lead_minutes=resolved_settings.default_reminder_lead_minutes,
        clarification_fallback_minutes=resolved_settings.clarification_fallback_minutes,
        approval_expiry_hours=resolved_settings.approval_expiry_hours,
    )
    media_text_service = MediaTextService(
        resolved_llm,
        resolved_settings.telegram_max_media_bytes,
    )
    resolved_openwa_client = openwa_client
    if resolved_openwa_client is None and resolved_settings.openwa_configured:
        assert resolved_settings.openwa_api_key is not None
        resolved_openwa_client = HttpOpenWAReadClient(
            resolved_settings.openwa_base_url,
            resolved_settings.openwa_api_key.get_secret_value(),
        )
    whatsapp_media_text_service = MediaTextService(
        resolved_llm,
        resolved_settings.whatsapp_max_media_bytes,
    )
    whatsapp_service = WhatsAppService(
        session_factory,
        intake_service,
        resolved_notifier,
        whatsapp_media_text_service,
        resolved_settings,
        clock,
        resolved_openwa_client,
    )
    reminder_service = ReminderService(
        session_factory=session_factory,
        notifier=resolved_notifier,
        lifecycle=lifecycle_service,
        llm_provider=resolved_llm,
        now=clock,
        timezone=resolved_settings.timezone,
        quiet_hours_start=resolved_settings.quiet_hours_start,
        quiet_hours_end=resolved_settings.quiet_hours_end,
        overdue_grace_minutes=resolved_settings.overdue_grace_minutes,
        default_reminder_lead_minutes=resolved_settings.default_reminder_lead_minutes,
    )
    confirmation_service = ConfirmationService(session_factory, clock, lifecycle_service)
    morning_brief_service = MorningBriefService(
        session_factory,
        resolved_notifier,
        resolved_calendar_provider,
        clock,
        resolved_settings.timezone,
        resolved_settings.morning_brief_fallback_time,
    )
    scheduler = SchedulerRuntime(
        reminder_service,
        morning_brief_service,
        lifecycle_service,
        control,
        resolved_settings.proactive_check_interval_seconds,
        whatsapp_service,
    )
    if telegram_runtime is not None:
        telegram_runtime.bind_services(
            intake_service,
            reminder_service,
            calendar_service,
            confirmation_service,
            morning_brief_service,
            media_text_service,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(
            resolved_settings.log_level,
            json_logs=resolved_settings.app_env.lower() == "production",
        )
        if resolved_settings.auto_create_schema:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        if telegram_runtime is not None:
            await telegram_runtime.start()
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await whatsapp_service.stop()
            if telegram_runtime is not None:
                await telegram_runtime.stop()
            if isinstance(resolved_llm, GeminiProvider):
                await resolved_llm.aclose()
            if resolved_openwa_client is not None:
                await resolved_openwa_client.aclose()
            await engine.dispose()

    application = FastAPI(title="Personal Agent", version="0.1.0", lifespan=lifespan)
    application.state.settings = resolved_settings
    application.state.clock = clock
    application.state.llm_provider = resolved_llm
    application.state.session_factory = session_factory
    application.state.intake_service = intake_service
    application.state.reminder_service = reminder_service
    application.state.lifecycle_service = lifecycle_service
    application.state.calendar_service = calendar_service
    application.state.confirmation_service = confirmation_service
    application.state.morning_brief_service = morning_brief_service
    application.state.media_text_service = media_text_service
    application.state.whatsapp_media_text_service = whatsapp_media_text_service
    application.state.whatsapp_service = whatsapp_service
    application.state.openwa_client = resolved_openwa_client
    application.state.telegram_runtime = telegram_runtime
    application.state.scheduler = scheduler
    application.state.control = control
    application.state.notifier = resolved_notifier
    application.include_router(health_router)
    application.include_router(briefs_router)
    application.include_router(openwa_router)
    application.include_router(shortcuts_router)
    application.include_router(status_router)
    return application


app = create_app()
