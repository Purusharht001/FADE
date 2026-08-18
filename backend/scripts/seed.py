"""CLI entrypoint: `uv run python scripts/seed.py`"""

import asyncio

import app.models  # noqa: F401 - registers models on Base.metadata
from app.db.base import Base
from app.db.seed import seed
from app.db.session import dispose_engine, get_engine, get_session_factory


async def main() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = get_session_factory()
    async with session_factory() as session:
        await seed(session)

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
