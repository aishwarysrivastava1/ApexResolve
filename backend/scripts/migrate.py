"""Tiny migration runner: applies migrations/NNNN_*.sql in order, each exactly once, as apex_owner.

Usage: MIGRATION_DATABASE_URL=postgresql://apex_owner:...@db/apex python scripts/migrate.py migrations
"""
import asyncio
import hashlib
import os
import pathlib
import sys
import asyncpg


async def migrate(dsn, folder):
    conn = await asyncpg.connect(dsn)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, sha256 CHAR(64) NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    applied = {}
    for row in await conn.fetch("SELECT name, sha256 FROM schema_migrations"):
        applied[row["name"]] = row["sha256"]
    files = sorted(pathlib.Path(folder).glob("[0-9][0-9][0-9][0-9]_*.sql"))
    for path in files:
        sql = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if path.name in applied:
            # an applied migration must never change; add a new file instead
            if applied[path.name] != digest:
                sys.exit(f"migration {path.name} was modified after being applied")
            continue
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations (name, sha256) VALUES ($1, $2)", path.name, digest)
        print(f"applied {path.name}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate(os.environ["MIGRATION_DATABASE_URL"], sys.argv[1]))
