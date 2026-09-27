"""Load synthetic demo data into the mock-core tables. Runs as apex_owner. Refuses to run when APP_ENV=prod.

Usage: MIGRATION_DATABASE_URL=postgresql://apex_owner:...@db/apex APP_ENV=dev python scripts/seed_demo.py seed/demo_seed.json
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import asyncpg


async def seed(dsn, data):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    today_ist = now.astimezone(ZoneInfo("Asia/Kolkata")).date()
    conn = await asyncpg.connect(dsn)
    async with conn.transaction():
        for m in data["merchants"]:
            await conn.execute("INSERT INTO merchants (merchant_id, name, return_postcode) VALUES ($1, $2, $3) "
                               "ON CONFLICT (merchant_id) DO NOTHING", m["merchant_id"], m["name"], m["return_postcode"])
            for se in m["se_numbers"]:
                await conn.execute("INSERT INTO merchant_se_numbers (se_number, merchant_id) VALUES ($1, $2) "
                                   "ON CONFLICT (se_number) DO NOTHING", se, m["merchant_id"])
        for a in data["accounts"]:
            await conn.execute(
                "INSERT INTO core_accounts (account_token, cardmember_ref, status, shipping_postcode, display_name, "
                "card_display_mask) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (account_token) DO NOTHING",
                a["account_token"], a["cardmember_ref"], a["status"], a["shipping_postcode"], a["display_name"],
                a["card_display_mask"])
        # charges first, then credits (a credit references its original charge)
        charges = [t for t in data["transactions"] if t["kind"] != "CREDIT"]
        credits = [t for t in data["transactions"] if t["kind"] == "CREDIT"]
        ordered = charges + credits
        for t in ordered:
            expected = None
            if "expected_delivery_days_ago" in t:
                expected = today_ist - timedelta(days=t["expected_delivery_days_ago"])
            await conn.execute(
                "INSERT INTO core_transactions (transaction_id, account_token, se_number, kind, amount_minor, currency, "
                "transaction_at, order_ref, expected_delivery_date, original_transaction_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) ON CONFLICT (transaction_id) DO NOTHING",
                t["transaction_id"], t["account_token"], t["se_number"], t["kind"], t["amount_minor"], t["currency"],
                now - timedelta(minutes=t["minutes_ago"]), t.get("order_ref"), expected,
                t.get("original_transaction_id"))
    await conn.close()


def main():
    if os.environ.get("APP_ENV") not in ("dev", "test", "demo"):
        sys.exit("refusing to seed demo data unless APP_ENV is dev, test or demo")
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        data = json.load(handle)
    asyncio.run(seed(os.environ["MIGRATION_DATABASE_URL"], data))
    print("demo data loaded")


if __name__ == "__main__":
    main()
