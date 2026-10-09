"""Safely verify the configured Oracle Autonomous Database connection."""

import asyncio

from sqlalchemy import text

from personal_agent.core.config import get_settings
from personal_agent.domain.database import create_engine


async def main() -> None:
    settings = get_settings()
    if not settings.database_url.startswith("oracle+"):
        raise SystemExit("DATABASE_URL must be oracle+oracledb_async://@")
    engine = create_engine(settings.database_url, settings.database_connect_args())
    try:
        async with engine.connect() as connection:
            result = await connection.scalar(text("SELECT 1 FROM DUAL"))
        if result != 1:
            raise SystemExit("Oracle connection succeeded but the verification query failed")
        print("Oracle connection verified successfully.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
