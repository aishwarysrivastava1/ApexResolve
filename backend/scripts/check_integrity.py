"""Run the integrity and reconciliation checks (APX-06 §7). Exit code 1 if any check finds a problem.

Usage (inside the api container: make check-integrity): python scripts/check_integrity.py
Uses DATABASE_URL (read access is enough).
"""
import asyncio
import os
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.services import integrity  # noqa: E402


async def main():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine)
    async with sessions() as session:
        problems = await integrity.check(session)
    await engine.dispose()
    for name, count in problems:
        print(f"FAILED: {name} ({count} rows)")
    if not problems:
        print(f"OK: all {len(integrity.CHECKS)} integrity checks passed")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    asyncio.run(main())
