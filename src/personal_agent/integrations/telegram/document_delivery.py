import errno
import re
from pathlib import Path
from typing import Protocol

from telegram import Message

CI_CD_DOCUMENT_FILENAME = "ci-cd-aapanel-architecture-he.md"
CI_CD_DOCUMENT_CAPTION = "📘 מדריך CI/CD ו־aaPanel של Matzpen"  # noqa: RUF001


class DocumentBot(Protocol):
    async def send_document(
        self,
        *,
        chat_id: int,
        document: Path,
        filename: str,
        caption: str,
    ) -> Message: ...


def is_ci_cd_document_request(text: str) -> bool:
    """Recognize a narrow request for the repository's CI/CD guide."""
    normalized = " ".join(text.casefold().split()).strip("?!., ")
    normalized = re.sub(r"ci\s*[-_/]?\s*cd", "ci cd", normalized)
    has_document = any(word in normalized for word in ("מסמך", "מדריך", "md"))
    asks_to_send = any(word in normalized for word in ("שלח", "תשלח", "שלחי", "תשלחי"))
    return asks_to_send and has_document and "ci cd" in normalized


def ci_cd_document_path(repository_root: Path | None = None) -> Path:
    """Return the one fixed document path; callers cannot supply a filename."""
    root = repository_root or Path(__file__).resolve().parents[4]
    return root.resolve() / "docs" / CI_CD_DOCUMENT_FILENAME


async def send_ci_cd_document(
    bot: DocumentBot,
    chat_id: int,
    repository_root: Path | None = None,
) -> str:
    """Send the fixed CI/CD guide through Telegram's send_document API."""
    document_path = ci_cd_document_path(repository_root)
    if not document_path.is_file():
        raise FileNotFoundError(errno.ENOENT, "CI/CD guide is missing", document_path)
    message = await bot.send_document(
        chat_id=chat_id,
        document=document_path,
        filename=CI_CD_DOCUMENT_FILENAME,
        caption=CI_CD_DOCUMENT_CAPTION,
    )
    return str(message.message_id)
