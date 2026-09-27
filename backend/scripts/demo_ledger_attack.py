"""DEMO ONLY: simulate an insider attack on the ledger so the auditor console can show detection.

  tamper   : change one event's payload (the trigger is disabled for a moment, as only the owner could)
  truncate : delete the last N events and rewind the head (and drop checkpoints pointing at them)

Refuses to run unless APP_ENV is dev or demo. Usage:
  APP_ENV=demo MIGRATION_DATABASE_URL=... python scripts/demo_ledger_attack.py tamper
  APP_ENV=demo MIGRATION_DATABASE_URL=... python scripts/demo_ledger_attack.py truncate 3
"""
import asyncio
import os
import sys
import asyncpg


async def tamper(conn):
    async with conn.transaction():
        await conn.execute("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        # flip the verdict of the most recent decision, as a dishonest insider might
        seq = await conn.fetchval("SELECT max(seq) FROM audit_events WHERE event_type = 'DECISION_RECORDED'")
        if seq is None:
            seq = await conn.fetchval("SELECT max(seq) FROM audit_events")
            await conn.execute("UPDATE audit_events SET actor = actor || '-edited' WHERE seq = $1", seq)
        else:
            await conn.execute(
                "UPDATE audit_events SET payload_canonical = CASE "
                "WHEN payload_canonical LIKE '%CARDMEMBER_REFUND%' "
                "THEN replace(payload_canonical, 'CARDMEMBER_REFUND', 'MERCHANT_UPHELD') "
                "ELSE replace(payload_canonical, 'MERCHANT_UPHELD', 'CARDMEMBER_REFUND') END WHERE seq = $1", seq)
        await conn.execute("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only")
    print(f"tampered with event seq {seq}")


async def truncate(conn, count):
    async with conn.transaction():
        await conn.execute("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        await conn.execute("ALTER TABLE ledger_checkpoints DISABLE TRIGGER ledger_checkpoints_append_only")
        last = await conn.fetchval("SELECT max(seq) FROM audit_events")
        keep = last - count
        await conn.execute("DELETE FROM ledger_checkpoints WHERE seq > $1", keep)
        await conn.execute("DELETE FROM audit_events WHERE seq > $1", keep)
        new_tip = await conn.fetchval("SELECT entry_hash FROM audit_events WHERE seq = $1", keep)
        await conn.execute("UPDATE ledger_head SET last_seq = $1, last_hash = $2 WHERE id = 1", keep, new_tip)
        await conn.execute("ALTER TABLE ledger_checkpoints ENABLE TRIGGER ledger_checkpoints_append_only")
        await conn.execute("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only")
    print(f"removed events {keep + 1}..{last}")


async def main():
    if os.environ.get("APP_ENV") not in ("dev", "demo"):
        sys.exit("refusing: demo attacks only run when APP_ENV is dev or demo")
    conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
    if sys.argv[1] == "tamper":
        await tamper(conn)
    elif sys.argv[1] == "truncate":
        await truncate(conn, int(sys.argv[2]) if len(sys.argv) > 2 else 3)
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
