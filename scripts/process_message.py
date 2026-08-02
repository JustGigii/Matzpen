import argparse
import asyncio
import hashlib

from personal_agent.core.config import Settings
from personal_agent.core.time import utc_now
from personal_agent.domain.database import Base, create_engine, create_session_factory
from personal_agent.domain.enums import EventDirection, EventSource
from personal_agent.domain.schemas import NormalizedEvent
from personal_agent.integrations.google_calendar.client import GoogleCalendarProvider
from personal_agent.integrations.llm.gemini import GeminiProvider
from personal_agent.integrations.telegram.text import TelegramTextNotifier
from personal_agent.services.intake import IntakeService
from personal_agent.services.lifecycle import LifecycleService
from personal_agent.services.policy import ApprovalPolicy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest one development message through the full durable workflow."
    )
    parser.add_argument(
        "message",
        nargs="*",
        help="Message text. When omitted, the script prompts for it.",
    )
    parser.add_argument(
        "--event-id",
        help=(
            "Stable source identifier; reuse it to verify deduplication. "
            "Defaults to a message hash."
        ),
    )
    return parser.parse_args()


async def run(message: str, event_id: str | None = None) -> None:
    settings = Settings()
    if not settings.gemini_configured:
        raise SystemExit("GEMINI_API_KEY and GEMINI_MODEL are not configured in .env")
    if not settings.telegram_configured:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS are not configured in .env"
        )
    assert settings.gemini_api_key is not None
    assert settings.gemini_model is not None
    assert settings.telegram_bot_token is not None

    llm_provider = GeminiProvider(
        settings.gemini_api_key.get_secret_value(),
        settings.gemini_model,
        settings.timezone,
    )
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    calendar_provider = None
    if settings.google_calendar_configured:
        assert settings.google_token_file is not None
        calendar_provider = GoogleCalendarProvider(
            settings.google_token_file,
            settings.google_calendar_id,
            settings.timezone,
        )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with TelegramTextNotifier(
            settings.telegram_bot_token.get_secret_value(),
            settings.telegram_allowed_user_ids[0],
            settings.timezone,
        ) as notifier:
            lifecycle = LifecycleService(
                session_factory,
                notifier,
                calendar_provider,
                utc_now,
                settings.default_reminder_lead_minutes,
            )
            service = IntakeService(
                session_factory=session_factory,
                llm_provider=llm_provider,
                notifier=notifier,
                policy=ApprovalPolicy(settings.internal_action_grace_seconds),
                lifecycle=lifecycle,
                now=utc_now,
                reminder_lead_minutes=settings.default_reminder_lead_minutes,
                clarification_fallback_minutes=settings.clarification_fallback_minutes,
                approval_expiry_hours=settings.approval_expiry_hours,
            )
            received_at = utc_now()
            stable_event_id = event_id or hashlib.sha256(message.encode()).hexdigest()
            result = await service.ingest(
                NormalizedEvent(
                    source=EventSource.DESKTOP,
                    source_account="process_message",
                    external_id=stable_event_id,
                    event_type="message.received",
                    direction=EventDirection.INBOUND,
                    occurred_at=received_at,
                    received_at=received_at,
                    content_text=message,
                    payload_json={"script": "process_message"},
                    dedupe_key=f"process_message:{stable_event_id}",
                )
            )
            print(
                "Ingested message; "
                f"commitments={len(result.commitment_ids)}, tasks={len(result.task_ids)}, "
                f"pending_actions={len(result.approval_ids)}"
            )
            if result.approval_ids:
                print(
                    "Keep the application running to handle Telegram callbacks "
                    "and scheduled actions."
                )
    finally:
        await llm_provider.aclose()
        await engine.dispose()


def main() -> None:
    args = parse_args()
    message = " ".join(args.message).strip()
    if not message:
        message = input("Message: ").strip()
    asyncio.run(run(message, args.event_id))


if __name__ == "__main__":
    main()
