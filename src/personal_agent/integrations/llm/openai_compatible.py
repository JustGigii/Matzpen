import asyncio
import base64
import json
import math
from collections.abc import Mapping
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ValidationError

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
)
from personal_agent.integrations.llm.gemini import (
    CHAT_INSTRUCTION,
    MEDIA_TEXT_INSTRUCTION,
    SYSTEM_INSTRUCTION,
    TIME_PROPOSAL_INSTRUCTION,
    relaxed_serving_schema,
)

SchemaModelT = TypeVar("SchemaModelT", bound=BaseModel)
IMAGE_MIME_TYPES = frozenset({"image/heic", "image/heif", "image/jpeg", "image/png", "image/webp"})
AUDIO_MIME_TYPES = frozenset(
    {
        "audio/aac",
        "audio/aiff",
        "audio/flac",
        "audio/mp3",
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
    }
)


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Adapt a Pydantic schema to strict OpenAI-compatible constrained decoding."""

    schema = _require_all_object_properties(relaxed_serving_schema(model))
    if not isinstance(schema, dict):
        raise TypeError("Model JSON schema must be an object")
    return schema


def _require_all_object_properties(value: Any) -> Any:
    if isinstance(value, list):
        return [_require_all_object_properties(item) for item in value]
    if not isinstance(value, dict):
        return value

    adapted = {key: _require_all_object_properties(child) for key, child in value.items()}
    if adapted.get("type") == "object" or "properties" in adapted:
        properties = adapted.get("properties")
        if isinstance(properties, dict):
            adapted["required"] = list(properties)
        adapted["additionalProperties"] = False
    return adapted


class OpenAICompatibleProvider:
    """Structured text, vision, and audio through an OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        text_model: str,
        timezone: str = "Asia/Jerusalem",
        *,
        vision_model: str | None = None,
        audio_model: str | None = None,
        max_attempts: int = 2,
        timeout_seconds: float = 90.0,
    ) -> None:
        if not api_key or not base_url or not text_model:
            raise ValueError("API key, base URL, and text model are required")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_seconds),
        )
        self._text_model = text_model
        self._vision_model = vision_model
        self._audio_model = audio_model
        self._timezone_name = timezone
        self._timezone = ZoneInfo(timezone)
        self._max_attempts = max_attempts

    async def extract_event(self, request: ExtractionRequest) -> ExtractionResult:
        prompt = (
            f"Event type: {request.event_type}\n"
            f"Direction: {request.direction.value}\n"
            f"Occurred at (UTC): {request.occurred_at.isoformat()}\n"
            f"User timezone: {self._timezone_name}\n"
            f"Occurred at (user local time): "
            f"{request.occurred_at.astimezone(self._timezone).isoformat()}\n"
            f"Conversation type: {request.conversation_type or 'unknown'}\n"
            f"Conversation label: {request.conversation_display_name or 'unknown'}\n"
            "Untrusted content follows between markers.\n"
            f"--- BEGIN UNTRUSTED CONTENT ---\n{request.content_text}\n"
            "--- END UNTRUSTED CONTENT ---"
        )
        return await self._structured_completion(
            SYSTEM_INSTRUCTION,
            prompt,
            ExtractionResult,
            schema_name="extraction_result",
            max_completion_tokens=4096,
            temperature=0,
        )

    async def propose_time(self, request: TimeProposalRequest) -> TimeProposal:
        prompt = (
            f"Purpose: {request.purpose}\n"
            f"Reference time: {request.reference_at.isoformat()}\n"
            f"User timezone: {self._timezone_name}\n"
            "Explicit date: "
            f"{request.explicit_date.isoformat() if request.explicit_date else 'none'}\n"
            f"Summary: {request.summary}\n"
            "--- BEGIN UNTRUSTED SOURCE ---\n"
            f"{request.source_text}\n"
            "--- END UNTRUSTED SOURCE ---"
        )
        return await self._structured_completion(
            TIME_PROPOSAL_INSTRUCTION,
            prompt,
            TimeProposal,
            schema_name="time_proposal",
            max_completion_tokens=1024,
            temperature=0,
        )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        context = request.model_dump_json(exclude={"message"})
        prompt = (
            "Bounded personal context supplied by the application:\n"
            f"{context}\n"
            "--- BEGIN UNTRUSTED USER MESSAGE ---\n"
            f"{request.message}\n"
            "--- END UNTRUSTED USER MESSAGE ---"
        )
        return await self._structured_completion(
            CHAT_INSTRUCTION,
            prompt,
            ChatResponse,
            schema_name="chat_response",
            max_completion_tokens=4096,
            temperature=0.3,
        )

    async def extract_media_text(
        self,
        content: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> str:
        if mime_type in AUDIO_MIME_TYPES:
            return await self._transcribe_audio(content, mime_type, filename)
        if mime_type in IMAGE_MIME_TYPES:
            return await self._extract_image_text(content, mime_type)
        raise LLMUnsupportedMediaError(mime_type)

    async def _structured_completion(
        self,
        system_instruction: str,
        prompt: str,
        response_model: type[SchemaModelT],
        *,
        schema_name: str,
        max_completion_tokens: int,
        temperature: float,
    ) -> SchemaModelT:
        payload: dict[str, Any] = {
            "model": self._text_model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_completion_tokens": max_completion_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": strict_json_schema(response_model),
                },
            },
        }
        response = await self._post_json("chat/completions", payload)
        try:
            message = response["choices"][0]["message"]
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty completion content")
            return response_model.model_validate(json.loads(content))
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            ValidationError,
        ) as exc:
            raise LLMServiceUnavailableError(30) from exc

    async def _extract_image_text(self, content: bytes, mime_type: str) -> str:
        if self._vision_model is None:
            raise LLMUnsupportedMediaError(mime_type)
        data_url = f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"
        payload = {
            "model": self._vision_model,
            "messages": [
                {"role": "system", "content": MEDIA_TEXT_INSTRUCTION},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": MEDIA_TEXT_INSTRUCTION},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            "temperature": 0,
            "max_completion_tokens": 4096,
        }
        response = await self._post_json("chat/completions", payload)
        try:
            extracted = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMServiceUnavailableError(30) from exc
        if not isinstance(extracted, str) or not extracted.strip():
            raise LLMServiceUnavailableError(30)
        return extracted.strip()

    async def _transcribe_audio(
        self,
        content: bytes,
        mime_type: str,
        filename: str | None,
    ) -> str:
        if self._audio_model is None:
            raise LLMUnsupportedMediaError(mime_type)
        extension = mime_type.partition("/")[2].replace("mpeg", "mp3") or "bin"
        upload_name = filename or f"audio.{extension}"
        response = await self._post_multipart(
            "audio/transcriptions",
            data={"model": self._audio_model, "response_format": "json", "temperature": "0"},
            files={"file": (upload_name, content, mime_type)},
        )
        extracted = response.get("text")
        if not isinstance(extracted, str) or not extracted.strip():
            raise LLMServiceUnavailableError(30)
        return extracted.strip()

    async def _post_json(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_attempts):
            try:
                response = await self._client.post(path, json=payload)
                return self._validated_response(response)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 >= self._max_attempts:
                    raise LLMServiceUnavailableError(30) from exc
                await asyncio.sleep(2**attempt)
            except LLMServiceUnavailableError:
                if attempt + 1 >= self._max_attempts:
                    raise
                await asyncio.sleep(2**attempt)
        raise LLMServiceUnavailableError(30)

    async def _post_multipart(
        self,
        path: str,
        *,
        data: Mapping[str, str],
        files: Mapping[str, tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        for attempt in range(self._max_attempts):
            try:
                response = await self._client.post(path, data=data, files=files)
                return self._validated_response(response)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 >= self._max_attempts:
                    raise LLMServiceUnavailableError(30) from exc
                await asyncio.sleep(2**attempt)
            except LLMServiceUnavailableError:
                if attempt + 1 >= self._max_attempts:
                    raise
                await asyncio.sleep(2**attempt)
        raise LLMServiceUnavailableError(30)

    @staticmethod
    def _validated_response(response: httpx.Response) -> dict[str, Any]:
        retry_after = _retry_after_seconds(response.headers.get("retry-after"))
        if response.status_code in {402, 429}:
            if response.status_code == 402 and retry_after is None:
                retry_after = 3600
            normalized = response.text.casefold()
            raise LLMQuotaExceededError(
                retry_after,
                daily_limit=(
                    response.status_code == 429
                    and ("per day" in normalized or "daily" in normalized or "tpd" in normalized)
                ),
            )
        if response.status_code in {408, 500, 502, 503, 504}:
            raise LLMServiceUnavailableError(retry_after or 30)
        response.raise_for_status()
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise LLMServiceUnavailableError(30)
        return parsed

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return max(1, math.ceil(float(value)))
    except ValueError:
        return None
