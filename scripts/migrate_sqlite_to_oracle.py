"""Copy the local SQLite application data into an empty migrated Oracle schema."""

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Table, func, inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from personal_agent.core.config import get_settings
from personal_agent.domain import models  # noqa: F401
from personal_agent.domain.database import Base, create_engine

DEFAULT_SOURCE_URL = "sqlite+aiosqlite:///./data/personal_agent.db"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy all application rows from SQLite to an empty Oracle schema."
    )
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument(
        "--replace-target",
        action="store_true",
        help="Atomically replace rows in the Oracle application tables.",
    )
    return parser.parse_args()


async def _table_names(connection: AsyncConnection) -> set[str]:
    return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))


async def _count(connection: AsyncConnection, table: Table) -> int:
    return int(await connection.scalar(select(func.count()).select_from(table)) or 0)


async def _read_rows(connection: AsyncConnection, table: Table) -> list[dict[str, Any]]:
    result = await connection.execute(select(table))
    return [dict(row) for row in result.mappings()]


def _batches(rows: Sequence[Mapping[str, Any]], size: int) -> Sequence[Sequence[Mapping[str, Any]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


async def migrate(source_url: str, batch_size: int, *, replace_target: bool = False) -> None:
    if batch_size < 1:
        raise SystemExit("--batch-size must be at least 1")
    settings = get_settings()
    if not settings.database_url.startswith("oracle+"):
        raise SystemExit("Set DATABASE_URL=oracle+oracledb_async://@ before migration")
    if not source_url.startswith("sqlite+"):
        raise SystemExit("The source URL must use an async SQLite driver")

    source = create_engine(source_url)
    target = create_engine(settings.database_url, settings.database_connect_args())
    tables = list(Base.metadata.sorted_tables)
    source_counts: dict[str, int] = {}
    deferred_supersedes: list[tuple[Any, Any]] = []
    try:
        async with source.connect() as source_connection, target.connect() as target_connection:
            source_names = await _table_names(source_connection)
            target_names = await _table_names(target_connection)
            expected = {table.name for table in tables}
            missing_source = expected - source_names
            missing_target = expected - target_names
            if missing_source:
                raise SystemExit(
                    "SQLite schema is incomplete; run its migrations first. Missing: "
                    + ", ".join(sorted(missing_source))
                )
            if missing_target:
                raise SystemExit(
                    "Oracle schema is not migrated. Run `alembic upgrade head` first. Missing: "
                    + ", ".join(sorted(missing_target))
                )
            populated = [table.name for table in tables if await _count(target_connection, table)]
            if populated and not replace_target:
                raise SystemExit(
                    "Oracle target is not empty; refusing to overwrite tables: "
                    + ", ".join(populated)
                )

        async with source.connect() as source_connection, target.begin() as target_connection:
            if populated:
                events = Base.metadata.tables["events"]
                await target_connection.execute(text("ALTER SESSION DISABLE PARALLEL DML"))
                superseding_ids = list(
                    await target_connection.scalars(
                        select(events.c.id).where(events.c.supersedes_event_id.is_not(None))
                    )
                )
                for event_id in superseding_ids:
                    await target_connection.execute(
                        update(events)
                        .prefix_with("/*+ NO_PARALLEL */", dialect="oracle")
                        .where(events.c.id == event_id)
                        .values(supersedes_event_id=None)
                    )
                for table in reversed(tables):
                    await target_connection.execute(
                        table.delete().prefix_with("/*+ NO_PARALLEL */", dialect="oracle")
                    )
            for table in tables:
                rows = await _read_rows(source_connection, table)
                source_counts[table.name] = len(rows)
                if table.name == "events":
                    for row in rows:
                        supersedes = row.get("supersedes_event_id")
                        if supersedes is not None:
                            deferred_supersedes.append((row["id"], supersedes))
                            row["supersedes_event_id"] = None
                for batch in _batches(rows, batch_size):
                    if batch:
                        await target_connection.execute(table.insert(), list(batch))

            events = Base.metadata.tables["events"]
            for event_id, supersedes_event_id in deferred_supersedes:
                await target_connection.execute(
                    update(events)
                    .where(events.c.id == event_id)
                    .values(supersedes_event_id=supersedes_event_id)
                )

        async with target.connect() as target_connection:
            mismatches: list[str] = []
            for table in tables:
                target_count = await _count(target_connection, table)
                source_count = source_counts[table.name]
                if target_count != source_count:
                    mismatches.append(f"{table.name}: source={source_count}, target={target_count}")
                print(f"{table.name}: {target_count} rows")
            if mismatches:
                raise SystemExit("Migration count verification failed: " + "; ".join(mismatches))
        print("SQLite to Oracle migration completed and row counts were verified.")
    finally:
        await source.dispose()
        await target.dispose()


async def main() -> None:
    args = parse_args()
    await migrate(args.source_url, args.batch_size, replace_target=args.replace_target)


if __name__ == "__main__":
    asyncio.run(main())
