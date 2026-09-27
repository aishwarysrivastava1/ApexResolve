# Concurrency on PostgreSQL 16 with the real schema, the app role and the real service functions:
# one winner per dispute, a linear ledger, and jobs claimed exactly once.
import asyncio, tempfile, uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
import asyncpg, pgserver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.paths import POLICY_PATH, ROLES_SQL, SCHEMA_SQL, pg_host
from app.domain.policy import load_policy
from app.domain import ledger_math as L
from app.services.common import Ctx, Conflict, lock_dispute, move, append_event, schedule_job
from app.services import worker_jobs

KEY = Ed25519PrivateKey.generate()
CTX = Ctx(policy=load_policy(POLICY_PATH), signing_key=KEY, key_id="k-test", core=None, carrier=None)
NOW = datetime.now(timezone.utc)


async def main():
    srv = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    srv.psql(open(ROLES_SQL).read())
    sock = pg_host(srv)
    raw = await asyncpg.connect(f"postgresql://apex_owner@/apex?host={sock}")
    await raw.execute(open(SCHEMA_SQL).read())
    await raw.execute("""INSERT INTO merchants VALUES ('M1','Acme','400001');
        INSERT INTO merchant_se_numbers VALUES ('1000000001','M1');
        INSERT INTO core_accounts VALUES ('a1','cm-1','OPEN','110001','A','371449*****8431');
        INSERT INTO ledger_keys (key_id, algorithm, public_key_pem) VALUES ('k-test','Ed25519','pem');""")
    for i in range(30):
        await raw.execute("INSERT INTO core_transactions VALUES ($1,'a1','1000000001','CHARGE',1000,'INR',now(),NULL,NULL,NULL)", f"T{i}")
    await raw.close()
    engine = create_async_engine(f"postgresql+asyncpg://apex_app@/apex?host={sock}", pool_size=30)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = []
    async with sessions() as s, s.begin():
        for i in range(30):
            did = uuid.uuid4()
            ids.append(did)
            await s.execute(text("""INSERT INTO disputes (dispute_id, transaction_id, cardmember_ref, account_token,
                merchant_id, se_number, reason_code, transaction_amount_minor, disputed_amount_minor, currency,
                transaction_at, claim_received_at, filing_deadline_at, state, state_entered_at, policy_version)
                VALUES (:d, :t, 'cm-1', 'a1', 'M1', '1000000001', 'C08', 1000, 1000, 'INR', now(), now(),
                now() + interval '120 days', 'AWAITING_MERCHANT', now(), '2026.09.1')"""), {"d": did, "t": f"T{i}"})
            await append_event(s, CTX, did, "DISPUTE_CREATED", "cardmember:cm-1", {"reason_code": "C08"}, NOW)

    async def contest(did):
        try:
            async with sessions() as s, s.begin():
                dispute = await lock_dispute(s, did)
                due = NOW + timedelta(days=7)
                await move(s, CTX, dispute, "MERCHANT_CONTESTED", "merchant:M1", NOW, due_at=due)
                await schedule_job(s, did, "REBUTTAL_DEADLINE", NOW - timedelta(seconds=1))
                return "ok"
        except Conflict:
            return "409"

    same = await asyncio.gather(*[contest(ids[0]) for _ in range(10)])
    different = await asyncio.gather(*[contest(d) for d in ids[1:]])
    async with sessions() as s:
        rows = (await s.execute(text("SELECT * FROM audit_events ORDER BY seq"))).mappings().all()
    events = [dict(r) | {"dispute_id": str(r["dispute_id"])} for r in rows]
    ok, msg = L.verify_chain(events, {"k-test": KEY.public_key()})
    # three workers drain the 30 due REBUTTAL_DEADLINE jobs concurrently
    results = await asyncio.gather(*[drain(sessions) for _ in range(3)])
    async with sessions() as s:
        done = (await s.execute(text("SELECT count(*) FROM jobs WHERE kind='REBUTTAL_DEADLINE' AND status='DONE'"))).scalar()
        # each deadline moves the case to READY_FOR_DECISION and schedules DECIDE; with no evidence the
        # margin is 0, so every case ends in a settlement offer (R6)
        ready = (await s.execute(text("SELECT count(*) FROM disputes WHERE state='SETTLEMENT_OFFERED'"))).scalar()
        rows = (await s.execute(text("SELECT * FROM audit_events ORDER BY seq"))).mappings().all()
    events = [dict(r) | {"dispute_id": str(r["dispute_id"])} for r in rows]
    ok2, msg2 = L.verify_chain(events, {"k-test": KEY.public_key()})
    ok, msg = ok and ok2, msg + " / after workers: " + msg2
    await engine.dispose()
    return Counter(same), Counter(different), ok, msg, sum(results), done, ready


async def drain(sessions):
    count = 0
    while await worker_jobs.process_one_job(sessions, CTX, NOW):
        count = count + 1
    return count


def test_concurrency():
    same, different, ok, msg, processed, done, ready = asyncio.run(main())
    print(same, different, msg, processed, done, ready)
    assert same == Counter({"ok": 1, "409": 9})
    assert different == Counter({"ok": 29})
    assert ok, msg
    assert processed == 60          # 30 deadlines + 30 decisions, each exactly once
    assert done == 30 and ready == 30
