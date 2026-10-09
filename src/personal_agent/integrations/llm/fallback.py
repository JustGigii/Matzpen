import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from math import ceil
from time import monotonic
from typing import TypeVar

from personal_agent.domain.schemas import (
    ChatRequest,
    ChatResponse,
    ExtractionRequest,
    ExtractionResult,
    TimeProposal,
    TimeProposalRequest,
)
from personal_agent.integrations.llm.base import (
    LLMProvider,
    LLMQuotaExceededError,
    LLMRetryableError,
    LLMServiceUnavailableError,
    LLMUnsupportedMediaError,
    LLMUnsupportedOperationError,
)

logger = logging.getLogger(__name__)
ResultT = TypeVar("ResultT")


class FallbackLLMProvider:
    """Try configured providers in order and cool down temporary failures."""

    def __init__(
        self,
        providers: Sequence[tuple[str, LLMProvider]],
        *,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        inline_retry_budget_seconds: int = 40,
    ) -> None:
        if not providers:
            raise ValueError("At least one LLM provider is required")
        self._providers = tuple(providers)
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()
        self._clock = clock
        self._sleep = sleep
        self._inline_retry_budget_seconds = inline_retry_budget_seconds

    async def extract_event(self, request: ExtractionRequest) -> ExtractionResult:
        return await self._call("extract_event", lambda provider: provider.extract_event(request))

    async def propose_time(self, request: TimeProposalRequest) -> TimeProposal:
        return await self._call("propose_time", lambda provider: provider.propose_time(request))

    async def chat(self, request: ChatRequest) -> ChatResponse:
        return await self._call("chat", lambda provider: provider.chat(request))

    async def extract_media_text(
        self,
        content: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> str:
        return await self._call(
            "extract_media_text",
            lambda provider: provider.extract_media_text(content, mime_type, filename),
        )

    async def _call(
        self,
        operation: str,
        invoke: Callable[[LLMProvider], Awaitable[ResultT]],
        retry_budget_seconds: float | None = None,
    ) -> ResultT:
        if retry_budget_seconds is None:
            retry_budget_seconds = float(self._inline_retry_budget_seconds)
        failures: list[LLMRetryableError] = []
        unsupported: LLMUnsupportedMediaError | LLMUnsupportedOperationError | None = None
        attempted = False
        now = self._clock()

        for provider_name, provider in self._providers:
            cooldown_key = (provider_name, operation)
            async with self._lock:
                cooldown_until = self._cooldowns.get(cooldown_key, 0.0)
            if cooldown_until > now:
                continue

            attempted = True
            try:
                result = await invoke(provider)
                if failures or unsupported is not None:
                    logger.info(
                        "llm_fallback_succeeded",
                        extra={"provider": provider_name, "operation": operation},
                    )
                return result
            except (LLMUnsupportedMediaError, LLMUnsupportedOperationError) as exc:
                unsupported = exc
                logger.info(
                    "llm_provider_capability_skipped",
                    extra={"provider": provider_name, "operation": operation},
                )
            except LLMRetryableError as exc:
                failures.append(exc)
                cooldown_seconds = self._cooldown_seconds(exc)
                async with self._lock:
                    self._cooldowns[cooldown_key] = self._clock() + cooldown_seconds
                logger.warning(
                    "llm_provider_fallback",
                    extra={
                        "provider": provider_name,
                        "operation": operation,
                        "error_type": type(exc).__name__,
                        "cooldown_seconds": cooldown_seconds,
                    },
                )

        if failures:
            retry_wait = self._shortest_remaining_cooldown(operation)
            padded_wait = retry_wait + 0.1
            if padded_wait <= retry_budget_seconds:
                logger.info(
                    "llm_fallback_waiting_for_retry",
                    extra={"operation": operation, "wait_seconds": retry_wait},
                )
                await self._sleep(padded_wait)
                return await self._call(
                    operation,
                    invoke,
                    retry_budget_seconds=retry_budget_seconds - padded_wait,
                )
            raise self._combined_failure(failures)
        if unsupported is not None:
            raise unsupported
        if not attempted:
            retry_after = self._shortest_remaining_cooldown(operation)
            padded_wait = retry_after + 0.1
            if padded_wait <= retry_budget_seconds:
                await self._sleep(padded_wait)
                return await self._call(
                    operation,
                    invoke,
                    retry_budget_seconds=retry_budget_seconds - padded_wait,
                )
            raise LLMServiceUnavailableError(retry_after)
        raise LLMServiceUnavailableError(30)

    def _shortest_remaining_cooldown(self, operation: str) -> int:
        now = self._clock()
        remaining = [
            max(1, ceil(until - now))
            for (_provider_name, provider_operation), until in self._cooldowns.items()
            if provider_operation == operation and until > now
        ]
        return min(remaining, default=30)

    @staticmethod
    def _cooldown_seconds(error: LLMRetryableError) -> int:
        if isinstance(error, LLMQuotaExceededError) and error.daily_limit:
            return 3600
        if error.retry_after_seconds is not None:
            return max(1, error.retry_after_seconds)
        return 30

    @staticmethod
    def _combined_failure(failures: Sequence[LLMRetryableError]) -> LLMRetryableError:
        retry_values = [
            failure.retry_after_seconds
            for failure in failures
            if failure.retry_after_seconds is not None
        ]
        retry_after = min(retry_values) if retry_values else None
        if all(isinstance(failure, LLMQuotaExceededError) for failure in failures):
            return LLMQuotaExceededError(
                retry_after,
                daily_limit=all(
                    isinstance(failure, LLMQuotaExceededError) and failure.daily_limit
                    for failure in failures
                ),
            )
        return LLMServiceUnavailableError(retry_after)

    async def aclose(self) -> None:
        for _provider_name, provider in self._providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()
