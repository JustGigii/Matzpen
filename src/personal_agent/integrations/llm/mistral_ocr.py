import asyncio
import base64
import math
from typing import Any

import httpx

from personal_agent.domain.schemas import (
    ChatRequest,
    ChatResponse,
    ExtractionRequest,
    ExtractionResult,
    TimeProposal,
    TimeProposalRequest,
)
from personal_agent.integrations.llm.base import (
    LLMQuotaExceededError,
    LLMServiceUnavailableError,
    LLMUnsupportedMediaError,
    LLMUnsupportedOperationError,
)

SUPPORTED_OCR_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "image/heic",
        "image/heif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


class MistralOCRProvider:
    """Media-only fallback for PDF and image OCR through Mistral Document AI."""

    def __init__(
        self,
        api_key: str,
        model: str = "mistral-ocr-latest",
        *,
        max_attempts: int = 2,
    ) -> None:
        if not api_key or not model:
            raise ValueError("Mistral API key and OCR model are required")
        self._model = model
        self._max_attempts = max_attempts
        self._client = httpx.AsyncClient(
            base_url="https://api.mistral.ai/v1/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(120.0),
        )

    async def extract_event(self, request: ExtractionRequest) -> ExtractionResult:
        del request
        raise LLMUnsupportedOperationError("event extraction")

    async def propose_time(self, request: TimeProposalRequest) -> TimeProposal:
        del request
        raise LLMUnsupportedOperationError("time proposal")

    async def chat(self, request: ChatRequest) -> ChatResponse:
        del request
        raise LLMUnsupportedOperationError("chat")

    async def extract_media_text(
        self,
        content: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> str:
        del filename
        if mime_type not in SUPPORTED_OCR_MIME_TYPES:
            raise LLMUnsupportedMediaError(mime_type)
        document_type = "document_url" if mime_type == "application/pdf" else "image_url"
        url_field = document_type
        data_url = f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"
        payload = {
            "model": self._model,
            "document": {"type": document_type, url_field: data_url},
            "include_image_base64": False,
        }
        response = await self._post(payload)
        pages = response.get("pages")
        if not isinstance(pages, list):
            raise LLMServiceUnavailableError(30)
        extracted = "\n\n".join(
            str(page.get("markdown", "")).strip()
            for page in pages
            if isinstance(page, dict) and page.get("markdown")
        ).strip()
        if not extracted:
            raise LLMServiceUnavailableError(30)
        return extracted

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_attempts):
            try:
                response = await self._client.post("ocr", json=payload)
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                if response.status_code in {402, 429}:
                    if response.status_code == 402 and retry_after is None:
                        retry_after = 3600
                    normalized = response.text.casefold()
                    raise LLMQuotaExceededError(
                        retry_after,
                        daily_limit=(
                            response.status_code == 429
                            and ("monthly" in normalized or "month" in normalized)
                        ),
                    )
                if response.status_code in {408, 500, 502, 503, 504}:
                    raise LLMServiceUnavailableError(retry_after or 30)
                response.raise_for_status()
                parsed = response.json()
                if not isinstance(parsed, dict):
                    raise LLMServiceUnavailableError(30)
                return parsed
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 >= self._max_attempts:
                    raise LLMServiceUnavailableError(30) from exc
                await asyncio.sleep(2**attempt)
            except LLMServiceUnavailableError:
                if attempt + 1 >= self._max_attempts:
                    raise
                await asyncio.sleep(2**attempt)
        raise LLMServiceUnavailableError(30)

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return max(1, math.ceil(float(value)))
    except ValueError:
        return None
