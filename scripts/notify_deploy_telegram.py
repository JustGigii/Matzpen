"""Send one Telegram notification after a successful production deployment."""

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from telegram import Bot

from personal_agent.core.config import get_settings
from personal_agent.integrations.telegram.document_delivery import (
    CI_CD_DOCUMENT_FILENAME,
    ci_cd_document_path,
)

REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
MARKER_ROOT = Path("/www/backup/Mazpen")


def deployment_caption(revision: str) -> str:
    """Build the concise answer delivered with the architecture guide."""
    return (
        "✅ סיימתי והעליתי את השינוי לייצור.\n\n"
        "הפקודות „תמחק הכל” ו„תציג לי ואז תמחק אותם” מוחקות עכשיו מיד את כל "  # noqa: RUF001
        "המשימות וההתחייבויות הפתוחות, ומציגות מה נמחק.\n\n"
        "אין צורך במודל חזק יותר: זו הייתה בעיית ניתוב ולוגיקה, ולכן הפקודות האלה "
        "מטופלות באופן ישיר ועקבי לפני פנייה למודל.\n\n"
        "מצורף מדריך ה־CI/CD והחיבור ל־aaPanel.\n"
        f"גרסה: {revision[:7]}"
    )


def _create_marker(marker: Path, revision: str) -> None:
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{revision}\n")


async def notify(revision: str) -> None:
    """Send the fixed guide once to the primary configured Telegram user."""
    if REVISION_PATTERN.fullmatch(revision) is None:
        raise ValueError("Invalid deployment revision")

    marker = MARKER_ROOT / f"telegram-notified-{revision}"
    if marker.exists():
        print(f"Telegram deployment notification already sent for {revision[:7]}")
        return

    settings = get_settings()
    if not settings.telegram_configured:
        raise RuntimeError("Telegram is not configured")
    telegram_token = settings.telegram_bot_token
    if telegram_token is None:
        raise RuntimeError("Telegram token is missing")

    document = ci_cd_document_path(Path.cwd())
    if not document.is_file():
        raise FileNotFoundError(document)

    async with Bot(telegram_token.get_secret_value()) as bot:
        await bot.send_document(
            chat_id=settings.telegram_allowed_user_ids[0],
            document=document,
            filename=CI_CD_DOCUMENT_FILENAME,
            caption=deployment_caption(revision),
        )

    try:
        _create_marker(marker, revision)
    except FileExistsError:
        pass
    print(f"Telegram deployment notification sent for {revision[:7]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("revision")
    args = parser.parse_args()
    try:
        asyncio.run(notify(args.revision))
    # Keep deploy logs generic so failures cannot expose Telegram credentials or URLs.
    except Exception:
        print("Telegram deployment notification failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
