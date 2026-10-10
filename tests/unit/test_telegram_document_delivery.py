from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from personal_agent.integrations.telegram.document_delivery import (
    CI_CD_DOCUMENT_CAPTION,
    CI_CD_DOCUMENT_FILENAME,
    ci_cd_document_path,
    is_ci_cd_document_request,
    send_ci_cd_document,
)


@pytest.mark.parametrize(
    "text",
    [
        "שלח לי את מסמך ה-CI/CD",
        "תשלח את מדריך ci cd",
        "שלחי לי את ה-md של CI_CD",
    ],
)
def test_ci_cd_document_request_recognizes_narrow_hebrew_phrases(text: str) -> None:
    assert is_ci_cd_document_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "שלח לי מסמך אחר",
        "מה זה CI/CD?",
        "תציג את המשימות",
    ],
)
def test_ci_cd_document_request_does_not_capture_unrelated_text(text: str) -> None:
    assert is_ci_cd_document_request(text) is False


async def test_send_ci_cd_document_uses_only_the_fixed_repository_document(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    document = docs / CI_CD_DOCUMENT_FILENAME
    document.write_text("# guide\n", encoding="utf-8")
    bot = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(message_id=42)))

    message_id = await send_ci_cd_document(bot, 123, tmp_path)

    assert message_id == "42"
    bot.send_document.assert_awaited_once_with(
        chat_id=123,
        document=document,
        filename=CI_CD_DOCUMENT_FILENAME,
        caption=CI_CD_DOCUMENT_CAPTION,
    )


async def test_send_ci_cd_document_reports_a_missing_fixed_document(tmp_path: Path) -> None:
    bot = SimpleNamespace(send_document=AsyncMock())

    with pytest.raises(FileNotFoundError) as exc_info:
        await send_ci_cd_document(bot, 123, tmp_path)

    assert Path(exc_info.value.filename) == ci_cd_document_path(tmp_path)
    bot.send_document.assert_not_awaited()
