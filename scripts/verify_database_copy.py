"""Compare every application row between local SQLite and configured Oracle."""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncConnection

from personal_agent.core.config import get_settings
from personal_agent.domain import models  # noqa: F401
from personal_agent.domain.database import Base, create_engine

SOURCE_URL = "sqlite+aiosqlite:///./data/personal_agent.db"


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return {"datetime": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _canonical(child) for key, child in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(child) for child in value]
    return value


async def _fingerprint(connection: AsyncConnection, table: Table) -> tuple[int, str]:
    result = await connection.execute(select(table))
    rows = [
        json.dumps(_canonical(dict(row)), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in result.mappings()
    ]
    rows.sort()
    digest = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    return len(rows), digest


async def _rows_by_primary_key(
    connection: AsyncConnection,
    table: Table,
) -> dict[str, dict[str, Any]]:
    primary_keys = list(table.primary_key.columns)
    result = await connection.execute(select(table))
    indexed: dict[str, dict[str, Any]] = {}
    for row in result.mappings():
        values = dict(row)
        key = json.dumps(
            [_canonical(values[column.name]) for column in primary_keys],
            ensure_ascii=False,
            sort_keys=True,
        )
        indexed[key] = values
    return indexed


async def _difference_summary(
    source: AsyncConnection,
    target: AsyncConnection,
    table: Table,
) -> str:
    source_rows = await _rows_by_primary_key(source, table)
    target_rows = await _rows_by_primary_key(target, table)
    differing: dict[str, int] = {}
    for key in source_rows.keys() & target_rows.keys():
        for column in table.columns:
            if _canonical(source_rows[key][column.name]) != _canonical(
                target_rows[key][column.name]
            ):
                differing[column.name] = differing.get(column.name, 0) + 1
    return ", ".join(f"{name}={count}" for name, count in sorted(differing.items()))


async def main() -> None:
    settings = get_settings()
    if not settings.database_url.startswith("oracle+"):
        raise SystemExit("The configured target must be Oracle")
    source = create_engine(SOURCE_URL)
    target = create_engine(settings.database_url, settings.database_connect_args())
    try:
        async with source.connect() as source_connection, target.connect() as target_connection:
            for table in Base.metadata.sorted_tables:
                source_count, source_digest = await _fingerprint(source_connection, table)
                target_count, target_digest = await _fingerprint(target_connection, table)
                if source_count != target_count or source_digest != target_digest:
                    summary = await _difference_summary(
                        source_connection,
                        target_connection,
                        table,
                    )
                    raise SystemExit(
                        f"Data verification failed for {table.name}; differing columns: {summary}"
                    )
                print(f"{table.name}: {source_count} rows verified")
        print("Every application row matches between SQLite and Oracle.")
    finally:
        await source.dispose()
        await target.dispose()


if __name__ == "__main__":
    asyncio.run(main())
