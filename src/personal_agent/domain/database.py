import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.oracle import CLOB, TIMESTAMP
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import DateTime, Text, TypeDecorator

from personal_agent.core.time import require_aware


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC and restore timezone awareness on dialects such as SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "oracle":
            return dialect.type_descriptor(TIMESTAMP(timezone=False))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        normalized = require_aware(value).astimezone(UTC)
        # Oracle DATE has no timezone component. Store normalized UTC and restore UTC on read.
        return normalized.replace(tzinfo=None) if dialect.name == "oracle" else normalized

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class JSONData(TypeDecorator[Any]):
    """Portable JSON storage for SQLite and Oracle SQLAlchemy 2.0."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "oracle":
            return dialect.type_descriptor(CLOB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        del dialect
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        del dialect
        if value is None or not isinstance(value, str):
            return value
        return json.loads(value)


class Base(DeclarativeBase):
    pass


def create_engine(
    database_url: str,
    connect_args: dict[str, str] | None = None,
) -> AsyncEngine:
    url = make_url(database_url)
    database = url.database
    if url.get_backend_name() == "sqlite" and database is not None and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(database_url, pool_pre_ping=True, connect_args=connect_args or {})


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
