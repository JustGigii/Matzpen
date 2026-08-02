from io import BytesIO
from zipfile import ZipFile

import pytest

from personal_agent.integrations.llm.fake import FakeLLMProvider
from personal_agent.services.media import (
    DOCX_MIME_TYPE,
    MediaAttachment,
    MediaTextService,
    MediaValidationError,
)


def attachment(
    content: bytes,
    mime_type: str,
    filename: str | None = None,
) -> MediaAttachment:
    return MediaAttachment(
        content=content,
        mime_type=mime_type,
        filename=filename,
        file_unique_id="telegram-file-1",
    )


async def test_local_text_and_docx_are_extracted_without_llm() -> None:
    provider = FakeLLMProvider()
    service = MediaTextService(provider, max_media_bytes=1_000_000)

    assert await service.extract_text(attachment("שלום עולם".encode(), "text/plain")) == "שלום עולם"

    document = BytesIO()
    with ZipFile(document, "w") as archive:
        archive.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
              <w:body><w:p><w:r><w:t>פגישה עם יובל</w:t></w:r></w:p></w:body>
            </w:document>""",
        )

    extracted = await service.extract_text(
        attachment(document.getvalue(), DOCX_MIME_TYPE, "meeting.docx")
    )

    assert extracted == "פגישה עם יובל"
    assert provider.media_requests == []


async def test_audio_pdf_and_images_use_fake_media_provider() -> None:
    provider = FakeLLMProvider(
        media_texts=["תזכיר לי להתקשר לדניאל", "מועד הגשה מחר", "פגישה ביום ראשון"]
    )
    service = MediaTextService(provider, max_media_bytes=1_000_000)

    audio = await service.extract_text(attachment(b"audio", "audio/ogg", "voice.ogg"))
    pdf = await service.extract_text(attachment(b"pdf", "application/pdf", "assignment.pdf"))
    image = await service.extract_text(attachment(b"image", "image/jpeg", "schedule.jpg"))

    assert audio == "תזכיר לי להתקשר לדניאל"
    assert pdf == "מועד הגשה מחר"
    assert image == "פגישה ביום ראשון"
    assert provider.media_requests == [
        ("audio/ogg", "voice.ogg", 5),
        ("application/pdf", "assignment.pdf", 3),
        ("image/jpeg", "schedule.jpg", 5),
    ]


async def test_media_validation_rejects_unsupported_or_oversized_files() -> None:
    service = MediaTextService(FakeLLMProvider(), max_media_bytes=4)

    with pytest.raises(MediaValidationError, match="size limit"):
        await service.extract_text(attachment(b"12345", "audio/ogg"))
    with pytest.raises(MediaValidationError, match="Unsupported"):
        await service.extract_text(attachment(b"data", "application/zip", "archive.zip"))
