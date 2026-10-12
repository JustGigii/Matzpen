"""Send one Telegram notification after a successful production deployment."""

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import httpx
from telegram import Bot

from personal_agent.core.config import get_settings
from personal_agent.integrations.telegram.document_delivery import (
    CI_CD_DOCUMENT_FILENAME,
    ci_cd_document_path,
)

REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
MARKER_ROOT = Path("/www/backup/Mazpen")


def deployment_caption(revision: str) -> str:
    """Build a release notification without reusing notes from an older change."""
    return (
        "✅ הפריסה לייצור הסתיימה ובדיקות השירות עברו.\n"
        "מצורף מדריך ה־CI/CD והחיבור ל־aaPanel.\n"
        f"גרסה: {revision[:7]}"
    )


def whatsapp_status_line() -> str:
    """Report observed integration state, without treating API health as WhatsApp health."""
    try:
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.get("http://127.0.0.1:8080/api/status")
            response.raise_for_status()
        status = response.json()["whatsapp"]
        if not status["configured"]:
            return "WhatsApp: לא מוגדר בשרת."
        if status["reachable"] and status["session_state"] == "connected":
            return "WhatsApp: החיבור פעיל לפי בדיקת השרת."
        return f"WhatsApp: דרושה בדיקה; מצב חיבור: {status['session_state']}."
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return "WhatsApp: מצב החיבור לא זמין לבדיקה."


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
            caption=f"{deployment_caption(revision)}\n{whatsapp_status_line()}",
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
