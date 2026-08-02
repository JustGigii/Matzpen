from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree

from personal_agent.integrations.llm.base import LLMProvider

MAX_EXTRACTED_TEXT_CHARS = 20_000
MAX_DOCX_XML_BYTES = 5_000_000
DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
LOCAL_TEXT_MIME_TYPES = frozenset(
    {
        "application/json",
        "text/csv",
        "text/markdown",
        "text/plain",
    }
)
GEMINI_MEDIA_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "audio/aac",
        "audio/aiff",
        "audio/flac",
        "audio/mp3",
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "image/heic",
        "image/heif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


class MediaValidationError(ValueError):
    pass


@dataclass(frozen=True)
class MediaAttachment:
    content: bytes
    mime_type: str
    filename: str | None
    file_unique_id: str


class MediaTextService:
    """Converts bounded Telegram media into untrusted text without persisting raw bytes."""

    def __init__(self, llm_provider: LLMProvider, max_media_bytes: int) -> None:
        self._llm_provider = llm_provider
        self._max_media_bytes = max_media_bytes

    async def extract_text(self, attachment: MediaAttachment) -> str:
        if not attachment.content:
            raise MediaValidationError("The media file is empty")
        if len(attachment.content) > self._max_media_bytes:
            raise MediaValidationError("The media file exceeds the configured size limit")

        mime_type = self._normalize_mime_type(attachment.mime_type, attachment.filename)
        if mime_type in LOCAL_TEXT_MIME_TYPES:
            text = self._decode_text(attachment.content)
        elif mime_type == DOCX_MIME_TYPE:
            text = self._extract_docx_text(attachment.content)
        elif mime_type in GEMINI_MEDIA_MIME_TYPES:
            text = await self._llm_provider.extract_media_text(
                attachment.content,
                mime_type,
                attachment.filename,
            )
        else:
            raise MediaValidationError(f"Unsupported media type: {mime_type}")

        normalized = "\n".join(line.rstrip() for line in text.strip().splitlines()).strip()
        if not normalized:
            raise MediaValidationError("No readable text was found in the media")
        return normalized[:MAX_EXTRACTED_TEXT_CHARS]

    @staticmethod
    def _normalize_mime_type(mime_type: str, filename: str | None) -> str:
        normalized = mime_type.lower().strip()
        extension = PurePath(filename).suffix.lower() if filename else ""
        if extension == ".docx":
            return DOCX_MIME_TYPE
        if extension in {".txt", ".md"}:
            return "text/plain"
        if extension == ".csv":
            return "text/csv"
        if extension == ".pdf":
            return "application/pdf"
        if normalized == "audio/mpeg":
            return "audio/mp3"
        return normalized

    @staticmethod
    def _decode_text(content: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-16", "cp1255"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise MediaValidationError("The text encoding is not supported")

    @staticmethod
    def _extract_docx_text(content: bytes) -> str:
        try:
            with ZipFile(BytesIO(content)) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > MAX_DOCX_XML_BYTES:
                    raise MediaValidationError("The DOCX document is too large after extraction")
                xml = archive.read(info)
        except (BadZipFile, KeyError) as exc:
            raise MediaValidationError("The DOCX document is invalid") from exc

        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise MediaValidationError("The DOCX document XML is invalid") from exc
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs: list[str] = []
        for paragraph in root.iter(f"{namespace}p"):
            text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
            if text.strip():
                paragraphs.append(text.strip())
        return "\n".join(paragraphs)
