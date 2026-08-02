from collections.abc import AsyncIterator, Callable

from fastapi import FastAPI
from httpx import AsyncClient


async def test_health_routes(
    app_factory: Callable[[None], FastAPI],
    client_for_app: Callable[[FastAPI], AsyncIterator[AsyncClient]],
) -> None:
    app = app_factory(None)
    async for client in client_for_app(app):
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        assert (await client.get("/health/ready")).json() == {"status": "ok"}
