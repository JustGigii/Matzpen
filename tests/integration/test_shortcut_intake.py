from collections.abc import AsyncIterator, Callable

from fastapi import FastAPI
from httpx import AsyncClient

from personal_agent.domain.schemas import ExtractionResult


async def test_shortcut_intake_is_authenticated_and_idempotent(
    app_factory: Callable[[list[ExtractionResult] | None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
) -> None:
    app = app_factory([ExtractionResult(language="he", items=[])])
    payload = {
        "type": "text",
        "content": "תזכיר לי להתקשר לדניאל",
        "captured_at": "2026-07-31T13:00:00+03:00",
        "metadata": {"source": "siri"},
    }
    async for client in client_for_app(app):
        unauthorized = await client.post("/api/intake/shortcut", json=payload)
        assert unauthorized.status_code == 401

        first = await client.post(
            "/api/intake/shortcut",
            json=payload,
            headers={"Authorization": "Bearer test-shortcut-token"},
        )
        assert first.status_code == 200
        assert first.json()["created"] is True

        duplicate = await client.post(
            "/api/intake/shortcut",
            json=payload,
            headers={"Authorization": "Bearer test-shortcut-token"},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["created"] is False
