"""Rewrap and verify user connection secrets with the configured primary key."""

import asyncio

from app.infrastructure.storage.postgres import get_postgres
from app.infrastructure.workbench.connection_keys import (
    connection_keys,
    rotate_connection_keys,
)
from core.config import get_settings


async def main():
    keys = connection_keys(get_settings())
    if keys is None:
        raise SystemExit("Configure the connection key ring first")
    db = get_postgres()
    await db.init()
    try:
        async with db.session_factory() as session, session.begin():
            count = await rotate_connection_keys(session, keys)
        print(f"Verified connection credentials. Re-encrypted {count} records.")
    finally:
        await db.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
