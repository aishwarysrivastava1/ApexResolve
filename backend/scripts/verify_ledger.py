"""Independent ledger verification for auditors: needs only read access and the public keys.

Usage: AUDIT_DATABASE_URL=postgresql://reader:...@host/apex python scripts/verify_ledger.py [checkpoint.json]
Inside the api container (make verify-ledger) it falls back to the app's own DATABASE_URL.
"""
import asyncio
import json
import os
import sys
import asyncpg
from cryptography.hazmat.primitives import serialization

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.domain import ledger_math as L  # noqa: E402


async def main():
    checkpoint = None
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as handle:
            checkpoint = json.load(handle)
    # read access is enough; the app's asyncpg URL works once the SQLAlchemy driver prefix is removed
    dsn = os.environ.get("AUDIT_DATABASE_URL") or os.environ["DATABASE_URL"].replace("postgresql+asyncpg://",
                                                                                     "postgresql://")
    conn = await asyncpg.connect(dsn)
    keys = {}
    for row in await conn.fetch("SELECT key_id, public_key_pem FROM ledger_keys"):
        keys[row["key_id"]] = serialization.load_pem_public_key(row["public_key_pem"].encode())
    rows = []
    for row in await conn.fetch("SELECT * FROM audit_events ORDER BY seq"):
        event = dict(row)
        event["dispute_id"] = str(event["dispute_id"])
        rows.append(event)
    await conn.close()
    ok, message = L.verify_chain(rows, keys, checkpoint)
    print(("OK: " if ok else "FAILED: ") + message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
