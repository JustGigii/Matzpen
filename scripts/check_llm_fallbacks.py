"""Verify configured fallback providers using synthetic, non-personal input."""

import argparse
import asyncio

import httpx

from personal_agent.core.config import get_settings
from personal_agent.domain.schemas import ChatRequest
from personal_agent.integrations.llm.mistral_ocr import MistralOCRProvider
from personal_agent.integrations.llm.openai_compatible import OpenAICompatibleProvider


def _selected_provider() -> str:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        choices=("all", "groq", "cerebras", "mistral"),
        default="all",
    )
    return str(parser.parse_args().provider)


def _verification_pdf() -> bytes:
    page_content = b"BT /F1 18 Tf 72 720 Td (Matzpen OCR test) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(page_content)).encode()
        + b" >>\nstream\n"
        + page_content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode())
        document.extend(body)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(document)


async def _check_text_provider(name: str, provider: OpenAICompatibleProvider) -> None:
    try:
        response = await provider.chat(
            ChatRequest(message="Synthetic connection test. Reply with one short sentence.")
        )
        if not response.reply.strip():
            raise RuntimeError(f"{name} returned an empty structured response")
        print(f"{name} structured text verified successfully.")
    finally:
        await provider.aclose()


async def _check_groq_models(api_key: str, expected_models: set[str]) -> None:
    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1/",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    ) as client:
        response = await client.get("models")
        response.raise_for_status()
        payload = response.json()
    available = {
        item.get("id")
        for item in payload.get("data", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    missing = expected_models - available
    if missing:
        raise RuntimeError("Groq models unavailable to this key: " + ", ".join(sorted(missing)))
    print("Groq text, vision, and audio model access verified successfully.")


async def main() -> None:
    settings = get_settings()
    selected = _selected_provider()
    if selected in {"all", "groq"} and settings.groq_configured:
        assert settings.groq_api_key is not None
        groq_key = settings.groq_api_key.get_secret_value()
        await _check_groq_models(
            groq_key,
            {
                settings.groq_text_model,
                settings.groq_vision_model,
                settings.groq_audio_model,
            },
        )
        await _check_text_provider(
            "Groq",
            OpenAICompatibleProvider(
                groq_key,
                "https://api.groq.com/openai/v1/",
                settings.groq_text_model,
                settings.timezone,
            ),
        )
    if selected in {"all", "cerebras"} and settings.cerebras_configured:
        assert settings.cerebras_api_key is not None
        await _check_text_provider(
            "Cerebras",
            OpenAICompatibleProvider(
                settings.cerebras_api_key.get_secret_value(),
                "https://api.cerebras.ai/v1/",
                settings.cerebras_model,
                settings.timezone,
            ),
        )
    if selected in {"all", "mistral"} and settings.mistral_configured:
        assert settings.mistral_api_key is not None
        provider = MistralOCRProvider(
            settings.mistral_api_key.get_secret_value(),
            settings.mistral_ocr_model,
        )
        try:
            extracted = await provider.extract_media_text(
                _verification_pdf(),
                "application/pdf",
                "synthetic-verification.pdf",
            )
            if "Matzpen" not in extracted:
                raise RuntimeError("Mistral OCR did not extract the synthetic marker")
            print("Mistral OCR verified successfully.")
        finally:
            await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
