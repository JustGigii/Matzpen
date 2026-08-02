from typing import Protocol

from personal_agent.domain.schemas import (
    ExtractionRequest,
    ExtractionResult,
    TimeProposal,
    TimeProposalRequest,
)


class LLMProvider(Protocol):
    async def extract_event(self, request: ExtractionRequest) -> ExtractionResult: ...

    async def propose_time(self, request: TimeProposalRequest) -> TimeProposal: ...

    async def extract_media_text(
        self,
        content: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> str: ...
