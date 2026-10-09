from typing import Protocol

from personal_agent.domain.schemas import (
    ChatRequest,
    ChatResponse,
    ExtractionRequest,
    ExtractionResult,
    TimeProposal,
    TimeProposalRequest,
)


class LLMRetryableError(RuntimeError):
    """A temporary provider failure that can be retried safely."""

    def __init__(self, message: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class LLMQuotaExceededError(LLMRetryableError):
    """A provider quota prevented the request from being processed."""

    def __init__(
        self,
        retry_after_seconds: int | None = None,
        *,
        daily_limit: bool = False,
    ) -> None:
        super().__init__("The configured LLM provider quota is exhausted", retry_after_seconds)
        self.daily_limit = daily_limit


class LLMServiceUnavailableError(LLMRetryableError):
    """The provider is temporarily overloaded or unavailable."""

    def __init__(self, retry_after_seconds: int | None = None) -> None:
        super().__init__(
            "The configured LLM provider is temporarily unavailable", retry_after_seconds
        )


class LLMUnsupportedMediaError(RuntimeError):
    """The provider cannot process the supplied media type."""

    def __init__(self, mime_type: str) -> None:
        super().__init__(f"The configured LLM provider does not support {mime_type}")
        self.mime_type = mime_type


class LLMUnsupportedOperationError(RuntimeError):
    """The provider is intentionally limited to a different LLM operation."""

    def __init__(self, operation: str) -> None:
        super().__init__(f"The configured LLM provider does not support {operation}")
        self.operation = operation


class LLMProvider(Protocol):
    async def extract_event(self, request: ExtractionRequest) -> ExtractionResult: ...

    async def propose_time(self, request: TimeProposalRequest) -> TimeProposal: ...

    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    async def extract_media_text(
        self,
        content: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> str: ...
