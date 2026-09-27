# T-CONC-01 (FR-FILE-10): many simultaneous filings for the same charge, with different and with identical
# Idempotency-Keys, create exactly ONE dispute. The others get 409 dispute-exists or a replay; never a 500.
import asyncio
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone

import asyncpg
import pgserver
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.paths import POLICY_PATH, ROLES_SQL, SCHEMA_SQL, pg_host
from app.domain import ledger_math as L
from app.domain.policy import load_policy
from app.gateways.carrier import MockCarrier
from app.gateways.core import MockCore
from app.services import disputes as disputes_svc
from app.services.common import Conflict, Ctx

KEY = Ed25519PrivateKey.generate()
CTX = Ctx(policy=load_policy(POLICY_PATH), signing_key=KEY, key_id="k-conc", core=MockCore(), carrier=MockCarrier({}))
ASHA = {"sub": "cm-asha", "role": "cardmember"}
NOW = datetime.now(timezone.utc)
BODY = {"transaction_id": "TXN-1", "reason_code": "C08", "disputed_amount_minor": 1000, "statement": "never came"}


async def main():
    server = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    server.psql(open(ROLES_SQL).read())
    sock = pg_host(server)
    raw = await asyncpg.connect(f"postgresql://apex_owner@/apex?host={sock}")
    await raw.execute(open(SCHEMA_SQL).read())
    await raw.execute("""INSERT INTO merchants VALUES ('M1','Acme','400001');
        INSERT INTO merchant_se_numbers VALUES ('1000000001','M1');
        INSERT INTO core_accounts VALUES ('a1','cm-asha','OPEN','110001','A','371449*****8431');""")
    await raw.execute("INSERT INTO core_transactions VALUES ('TXN-1','a1','1000000001','CHARGE',1000,'INR',$1,NULL,$2,NULL)",
                      NOW - timedelta(days=3), (NOW - timedelta(days=1)).date())
    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    await raw.execute("INSERT INTO ledger_keys (key_id, algorithm, public_key_pem) VALUES ('k-conc','Ed25519',$1)", pem)
    await raw.close()
    engine = create_async_engine(f"postgresql+asyncpg://apex_app@/apex?host={sock}", pool_size=20)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def attempt(key):
        # one HTTP request = one transaction; report how it ended
        try:
            async with sessions() as session, session.begin():
                _status, _dispute_id, replayed = await disputes_svc.file_dispute(session, CTX, ASHA, BODY, key, NOW)
                return "replay" if replayed else "created"
        except Conflict as conflict:
            return conflict.problem

    keys = [f"different-key-{i:02d}" for i in range(10)] + ["the-same-key-01"] * 5
    outcomes = Counter(await asyncio.gather(*[attempt(key) for key in keys]))
    async with sessions() as session:
        disputes = (await session.execute(text("SELECT count(*) FROM disputes"))).scalar()
        created_events = (await session.execute(text(
            "SELECT count(*) FROM audit_events WHERE event_type = 'DISPUTE_CREATED'"))).scalar()
        rows = (await session.execute(text("SELECT * FROM audit_events ORDER BY seq"))).mappings().all()
    events = [dict(r) | {"dispute_id": str(r["dispute_id"])} for r in rows]
    ok, message = L.verify_chain(events, {"k-conc": KEY.public_key()})
    await engine.dispose()
    return outcomes, disputes, created_events, ok, message


def test_concurrent_filings_create_one_dispute():
    outcomes, disputes, created_events, ok, message = asyncio.run(main())
    assert outcomes["created"] == 1, outcomes
    assert set(outcomes) <= {"created", "replay", "dispute-exists"}, outcomes
    assert sum(outcomes.values()) == 15
    assert disputes == 1 and created_events == 1
    assert ok, message
