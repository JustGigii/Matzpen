"""Send UTF-safe Hebrew smoke-test messages to the configured Telegram user."""

import asyncio

from personal_agent.core.config import get_settings
from personal_agent.integrations.telegram.smoke import validated_test_messages
from personal_agent.integrations.telegram.text import TelegramTextNotifier


async def run() -> None:
    settings = get_settings()
    if not settings.telegram_configured:
        raise RuntimeError("Telegram is not configured")
    assert settings.telegram_bot_token is not None
    async with TelegramTextNotifier(
        settings.telegram_bot_token.get_secret_value(),
        settings.telegram_allowed_user_ids[0],
        settings.timezone,
    ) as notifier:
        message_ids = [await notifier.send_text(message) for message in validated_test_messages()]
    print(f"sent_count={len(message_ids)}")
    print(f"message_ids={','.join(message_ids)}")


if __name__ == "__main__":
    asyncio.run(run())
