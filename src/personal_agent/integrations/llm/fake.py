from collections import deque

from personal_agent.domain.schemas import (
    ExtractionRequest,
    ExtractionResult,
    TimeProposal,
    TimeProposalRequest,
)


class FakeLLMProvider:
    """Deterministic provider for local development and tests; it never calls a network."""

    def __init__(
        self,
        results: list[ExtractionResult] | None = None,
        time_proposals: list[TimeProposal] | None = None,
        media_texts: list[str] | None = None,
    ) -> None:
        self._results = deque(results or [])
        self._time_proposals = deque(time_proposals or [])
        self._media_texts = deque(media_texts or [])
        self.requests: list[ExtractionRequest] = []
        self.time_requests: list[TimeProposalRequest] = []
        self.media_requests: list[tuple[str, str | None, int]] = []

    async def extract_event(self, request: ExtractionRequest) -> ExtractionResult:
        self.requests.append(request)
        if self._results:
            return self._results.popleft()
        return ExtractionResult(language="und", items=[])

    async def propose_time(self, request: TimeProposalRequest) -> TimeProposal:
        self.time_requests.append(request)
        if not self._time_proposals:
            raise RuntimeError("Fake LLM has no queued time proposal")
        return self._time_proposals.popleft()

    async def extract_media_text(
        self,
        content: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> str:
        self.media_requests.append((mime_type, filename, len(content)))
        if not self._media_texts:
            raise RuntimeError("Fake LLM has no queued media text")
        return self._media_texts.popleft()
