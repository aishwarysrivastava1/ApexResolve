# More service flows on real PostgreSQL 16 (complements test_e2e_services.py):
#   T-INT-FILE-05…09  refusals: not a charge, bad amount, open-dispute quota; velocity risk flag (T-INT-FILE-12)
#   T-INT-EVID-05, 09, 11, 12  return tracking, oversized file (413), tracking validation, per-side limit
#   T-INT-MERCH-03, T-INT-CM-02  merchant accepts, cardmember withdraws
#   T-INT-OFFER-02…04  offer declined → review; offer expired → review
#   T-INT-REV-01…03  review queue order and content, reviewer decision validation
#   T-INT-SETTLE-04  a failing core transfer is retried with backoff, then DEAD + reviewers notified
#   T-INT-LED-07  automatic checkpoints; T-INT-NOTIF-01 notifications for each party
#   T-INT-JOB-06  a timer that keeps failing becomes FAILED after 10 attempts and reviewers are notified
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

import asyncpg
import pgserver
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.paths import POLICY_PATH, ROLES_SQL, SCHEMA_SQL, pg_host
from app.domain.policy import load_policy
from app.gateways.carrier import MockCarrier
from app.gateways.core import MockCore
from app.services import actions as actions_svc
from app.services import disputes as disputes_svc
from app.services import evidence as evidence_svc
from app.services import worker_jobs as worker_svc
from app.services.common import Conflict, Ctx, Invalid, RateLimited

POLICY = load_policy(POLICY_PATH)
KEY = Ed25519PrivateKey.generate()
KID = "ledger-more-1"
CTX = Ctx(policy=POLICY, signing_key=KEY, key_id=KID, core=MockCore(),
          carrier=MockCarrier({"BLUEDART:AWB10000001": {"status": "DELIVERED", "postcode": "110001"},
                               "INDIA_POST:RET20000001": {"status": "DELIVERED", "postcode": "400001"}}))
ASHA = {"sub": "cm-asha", "role": "cardmember"}
RAVI = {"sub": "cm-ravi", "role": "cardmember"}
ACME = {"sub": "mer-acme", "role": "merchant", "merchant_id": "M_ACME"}
NEHA = {"sub": "rev-neha", "role": "reviewer"}
T0 = datetime.now(timezone.utc).replace(microsecond=0)
IST = timezone(timedelta(hours=5, minutes=30))
F = None
# APX-12 §9.2: seconds from entering READY_FOR_DECISION to the next state change, 95th percentile
DECISION_LATENCY_SQL = """
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM
       (b.created_at_text::timestamptz - a.created_at_text::timestamptz)))
FROM audit_events a
JOIN audit_events b ON b.dispute_id = a.dispute_id AND b.seq > a.seq AND b.event_type = 'STATE_CHANGED'
WHERE a.event_type = 'STATE_CHANGED' AND a.payload_canonical::jsonb ->> 'to' = 'READY_FOR_DECISION'
  AND NOT EXISTS (SELECT 1 FROM audit_events m WHERE m.dispute_id = a.dispute_id AND m.event_type = 'STATE_CHANGED'
                  AND m.seq > a.seq AND m.seq < b.seq)"""


class FailingCore(MockCore):
    """A core system that is down: every transfer raises."""

    async def transfer(self, *args, **kwargs):
        raise ConnectionError("core unavailable")


async def setup(sock):
    raw = await asyncpg.connect(f"postgresql://apex_owner@/apex?host={sock}")
    await raw.execute(open(SCHEMA_SQL).read())
    await raw.execute("""
      INSERT INTO merchants VALUES ('M_ACME','Acme Electronics','400001');
      INSERT INTO merchant_se_numbers VALUES ('1000000001','M_ACME');
      INSERT INTO core_accounts VALUES ('acct_asha_01','cm-asha','OPEN','110001','Asha Verma','371449*****8431'),
                                       ('acct_ravi_01','cm-ravi','OPEN','560001','Ravi Menon','378282*****0005');""")
    delivered = (T0.astimezone(IST) - timedelta(days=5)).date()
    for i in range(1, 7):     # Asha: six C08 purchases
        await raw.execute("INSERT INTO core_transactions VALUES ($1,'acct_asha_01','1000000001','CHARGE',499900,'INR',$2,$3,$4,NULL)",
                          f"TXN-A{i}", T0 - timedelta(days=12, minutes=i), f"ORD-A{i}", delivered)
    for i in range(1, 7):     # Ravi: six C31 purchases
        await raw.execute("INSERT INTO core_transactions VALUES ($1,'acct_ravi_01','1000000001','CHARGE',100000,'INR',$2,$3,NULL,NULL)",
                          f"TXN-R{i}", T0 - timedelta(days=9, minutes=i), f"ORD-R{i}")
    await raw.execute("INSERT INTO core_transactions VALUES ('TXN-R-CREDIT','acct_ravi_01','1000000001','CREDIT',100000,'INR',$1,'ORD-R1',NULL,'TXN-R1')",
                      T0 - timedelta(days=2))
    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    await raw.execute("INSERT INTO ledger_keys (key_id, algorithm, public_key_pem) VALUES ($1, 'Ed25519', $2)", KID, pem)
    await raw.close()


async def run(fn, *args, ctx=CTX):
    async with F() as session, session.begin():
        return await fn(session, ctx, *args)


async def one(sql, **params):
    async with F() as session:
        return (await session.execute(text(sql), params)).mappings().one()


async def expect(exc_type, coro, problem=None):
    try:
        await coro
    except exc_type as error:
        assert problem is None or error.problem == problem, (error.problem, problem)
        return error
    raise AssertionError(f"expected {exc_type.__name__}")


def body(txn, code="C08", amount=499900):
    return {"transaction_id": txn, "reason_code": code, "disputed_amount_minor": amount, "statement": "It never came."}


async def file(principal, txn, key, code="C08", amount=499900, now=T0):
    _status, dispute_id, _replayed = await run(disputes_svc.file_dispute, principal, body(txn, code, amount), key, now)
    return dispute_id


async def to_offer(txn, key):
    # statements on both sides give a margin of 0, so the case ends in a settlement offer (G05)
    dispute_id = await file(ASHA, txn, key)
    await run(evidence_svc.add_evidence, ACME, dispute_id,
              {"kind": "text", "evidence_type": "merchant_statement", "note": "We shipped it."}, None, T0)
    await run(actions_svc.contest, ACME, dispute_id, T0)
    await run(actions_svc.rebuttal_done, ASHA, dispute_id, T0)
    while await worker_svc.process_one_job(F, CTX, T0):      # run every job due now (including this DECIDE)
        pass
    assert (await one("SELECT state FROM disputes WHERE dispute_id = :d", d=dispute_id))["state"] == "SETTLEMENT_OFFERED"
    return dispute_id


async def main():
    global F
    server = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    server.psql(open(ROLES_SQL).read())
    sock = pg_host(server)
    await setup(sock)
    engine = create_async_engine(f"postgresql+asyncpg://apex_app@/apex?host={sock}")
    F = async_sessionmaker(engine, expire_on_commit=False)

    # ---- T-INT-FILE-05…09 refusals and the velocity risk flag (Ravi, C31)
    await expect(Invalid, file(RAVI, "TXN-R-CREDIT", "ravi-credit-1", "C31", 100000), "not-a-charge")
    await expect(Invalid, file(RAVI, "TXN-R1", "ravi-zero-01", "C31", 100001), "amount-invalid")
    # each filing happens a minute after the previous one (the flag counts disputes filed before "now")
    ravi = [await file(RAVI, f"TXN-R{i}", f"ravi-file-{i:02d}", "C31", 100000, T0 + timedelta(minutes=i))
            for i in range(1, 6)]
    flags = [(await one("SELECT risk_flag FROM disputes WHERE dispute_id = :d", d=d))["risk_flag"] for d in ravi]
    assert flags == [None, None, None, "HIGH_DISPUTE_VELOCITY", "HIGH_DISPUTE_VELOCITY"], flags
    await expect(Invalid, file(RAVI, "TXN-R6", "ravi-file-06", "C31", 100000, T0 + timedelta(minutes=6)),
                 "too-many-open-disputes")

    # ---- T-INT-EVID-05 a return delivered to the merchant's return address becomes verified cardmember evidence
    await run(evidence_svc.add_evidence, RAVI, ravi[0],
              {"kind": "return_tracking", "carrier": "INDIA_POST", "tracking_number": "RET20000001"}, None, T0)
    returned = await one("SELECT evidence_type, source, side, submitted_by FROM evidence_items "
                         "WHERE dispute_id = :d ORDER BY seq DESC LIMIT 1", d=ravi[0])
    assert dict(returned) == {"evidence_type": "return_shipment", "source": "system_verified",
                              "side": "CARDMEMBER", "submitted_by": "SYSTEM"}, dict(returned)

    # ---- T-INT-EVID-09, 11, 12 on an Asha C08 dispute
    d1 = await file(ASHA, "TXN-A1", "asha-file-01")
    big = b"%PDF-1.7\n" + b"0" * (POLICY["limits"]["max_file_bytes"] + 1)
    error = await expect(evidence_svc.UploadError, run(evidence_svc.add_evidence, ACME, d1,
                         {"kind": "file", "evidence_type": "signed_proof_of_delivery"}, big, T0))
    assert error.status == 413
    await expect(Invalid, run(evidence_svc.add_evidence, ACME, d1,
                 {"kind": "shipment_tracking", "carrier": "FEDEX", "tracking_number": "AWB10000001"}, None, T0),
                 "unsupported-carrier")
    await expect(Invalid, run(evidence_svc.add_evidence, ACME, d1,
                 {"kind": "shipment_tracking", "carrier": "BLUEDART", "tracking_number": "AWB 1; DROP"}, None, T0))
    await expect(Invalid, run(evidence_svc.add_evidence, ASHA, d1,
                 {"kind": "shipment_tracking", "carrier": "BLUEDART", "tracking_number": "AWB10000001"}, None, T0),
                 "evidence-not-allowed")
    # T-INT-EVID-06: a type from another reason code's catalogue (C31) is refused on this C08 dispute
    await expect(Invalid, run(evidence_svc.add_evidence, ACME, d1,
                 {"kind": "file", "evidence_type": "description_match_proof"}, b"%PDF-1.7\n", T0),
                 "evidence-not-allowed")
    for i in range(POLICY["limits"]["max_evidence_items_per_side"]):
        await run(evidence_svc.add_evidence, ACME, d1,
                  {"kind": "text", "evidence_type": "merchant_statement", "note": f"note {i}"}, None, T0)
    await expect(Invalid, run(evidence_svc.add_evidence, ACME, d1,
                 {"kind": "text", "evidence_type": "merchant_statement", "note": "one too many"}, None, T0),
                 "evidence-limit-reached")

    # ---- T-INT-MERCH-03 accept → refund decision (not appealable); T-INT-CM-02 withdraw → WITHDRAWN
    await run(actions_svc.accept, ACME, d1, T0)
    accepted = await one("SELECT d.state, x.verdict, x.rule_id, x.refund_amount_minor, x.appealable FROM disputes d "
                         "JOIN decisions x ON x.decision_id = d.current_decision_id WHERE d.dispute_id = :d", d=d1)
    assert dict(accepted) == {"state": "DECIDED", "verdict": "CARDMEMBER_REFUND", "rule_id": "MA_MERCHANT_ACCEPTED",
                              "refund_amount_minor": 499900, "appealable": False}, dict(accepted)
    d2 = await file(ASHA, "TXN-A2", "asha-file-02")
    await run(actions_svc.withdraw, ASHA, d2, T0)
    withdrawn = await one("SELECT x.verdict, x.refund_amount_minor FROM disputes d "
                          "JOIN decisions x ON x.decision_id = d.current_decision_id WHERE d.dispute_id = :d", d=d2)
    assert (withdrawn["verdict"], withdrawn["refund_amount_minor"]) == ("WITHDRAWN", 0)
    await expect(Conflict, run(actions_svc.withdraw, ASHA, d2, T0))          # not allowed any more

    # ---- T-INT-OFFER-02 declined → HUMAN_REVIEW; T-INT-OFFER-04 expired → HUMAN_REVIEW
    d3 = await to_offer("TXN-A3", "asha-file-03")
    await run(actions_svc.offer_response, ACME, d3, "DECLINED", T0)
    d4 = await to_offer("TXN-A4", "asha-file-04")
    await run(actions_svc.offer_response, ASHA, d4, "ACCEPTED", T0)
    after_offer = T0 + timedelta(days=POLICY["windows"]["settlement_offer_days"], minutes=1)
    while await worker_svc.process_one_job(F, CTX, after_offer):
        pass
    reasons = [(await one("SELECT state, status_reason FROM disputes WHERE dispute_id = :d", d=d)) for d in (d3, d4)]
    assert [(r["state"], r["status_reason"]) for r in reasons] == [("HUMAN_REVIEW", "OFFER_DECLINED"),
                                                                    ("HUMAN_REVIEW", "OFFER_EXPIRED")], reasons
    told = await one("SELECT count(*) AS n FROM notifications WHERE dispute_id = :d AND recipient_role IN "
                     "('CARDMEMBER', 'MERCHANT') AND message LIKE 'The settlement offer expired%'", d=d4)
    assert told["n"] == 2                                  # both parties learn that the offer expired

    # ---- T-INT-REV-01…03 queue order, validation and decision
    async with F() as session:
        queue = (await session.execute(text(
            "SELECT dispute_id FROM disputes WHERE state = 'HUMAN_REVIEW' ORDER BY state_entered_at"))).scalars().all()
    assert queue == [d3, d4], queue
    await expect(Invalid, run(actions_svc.review_decision, NEHA, d3, "SPLIT_SETTLEMENT", 499900,
                              "The evidence is balanced on both sides.", after_offer))
    await expect(Invalid, run(actions_svc.review_decision, NEHA, d3, "MERCHANT_UPHELD", None, "too short", after_offer))
    await run(actions_svc.review_decision, NEHA, d3, "SPLIT_SETTLEMENT", 200000,
              "The evidence is balanced on both sides.", after_offer)
    reviewed = await one("SELECT x.verdict, x.refund_amount_minor, x.decided_by, x.appealable FROM disputes d "
                         "JOIN decisions x ON x.decision_id = d.current_decision_id WHERE d.dispute_id = :d", d=d3)
    assert dict(reviewed) == {"verdict": "SPLIT_SETTLEMENT", "refund_amount_minor": 200000, "decided_by": "rev-neha",
                              "appealable": False}, dict(reviewed)

    # ---- T-INT-SETTLE-04 retries with backoff, then DEAD and reviewers notified
    failing = Ctx(policy=POLICY, signing_key=KEY, key_id=KID, core=FailingCore(), carrier=CTX.carrier)
    moment = after_offer
    while await worker_svc.process_one_job(F, failing, moment):       # FINALIZE jobs queue the transfers
        pass
    attempts = 0
    while attempts < 12:
        moment = moment + timedelta(seconds=301)
        if not await worker_svc.relay_one_outbox(F, failing, moment):
            break
        attempts = attempts + 1
    dead = await one("SELECT count(*) AS n FROM outbox WHERE status = 'DEAD' AND attempts = 10")
    alerts = await one("SELECT count(*) AS n FROM notifications WHERE recipient_role = 'REVIEWER' "
                       "AND message LIKE 'Transfer failed permanently%'")
    assert dead["n"] >= 1 and alerts["n"] == dead["n"], (dead["n"], alerts["n"])
    no_transfers = await one("SELECT count(*) AS n FROM core_transfers")
    assert no_transfers["n"] == 0

    # ---- T-INT-LED-07 automatic checkpoints (every N events)
    first = await worker_svc.maybe_auto_checkpoint(F, CTX, moment, every=10)
    again = await worker_svc.maybe_auto_checkpoint(F, CTX, moment, every=10)
    assert first is not None and again is None
    async with F() as session:
        ok, message = await worker_svc.verify_ledger(session, {KID: KEY.public_key()}, first)
    assert ok, message

    # ---- T-INT-NOTIF-01 each party is told what happened
    counts = {}
    async with F() as session:
        rows = (await session.execute(text(
            "SELECT recipient_role, count(*) AS n FROM notifications GROUP BY recipient_role"))).all()
    for row in rows:
        counts[row.recipient_role] = row.n
    assert counts.get("MERCHANT", 0) >= 8 and counts.get("CARDMEMBER", 0) >= 6 and counts.get("REVIEWER", 0) >= 3, counts

    # ---- the T-PERF-02 measurement query of APX-12 §9.2 runs and yields a p95 (simulated clock: 0 s)
    async with F() as session:
        p95 = (await session.execute(text(DECISION_LATENCY_SQL))).scalar()
    assert p95 is not None and p95 < 5, p95

    # ---- the daily filing quota is enforced before anything else is written (T-SEC-21, service level)
    many = dict(POLICY, limits=dict(POLICY["limits"], max_filings_per_day=2))
    tight = Ctx(policy=many, signing_key=KEY, key_id=KID, core=CTX.core, carrier=CTX.carrier)
    await expect(RateLimited, run(disputes_svc.file_dispute, ASHA, body("TXN-A5"), "asha-file-05", T0, ctx=tight))

    # ---- T-INT-JOB-06 a broken handler (no signing key) is retried with backoff, then FAILED + reviewers told
    d6 = await file(ASHA, "TXN-A6", "asha-file-06")
    broken = Ctx(policy=POLICY, signing_key=None, key_id=KID, core=CTX.core, carrier=CTX.carrier)
    clock = T0 + timedelta(days=POLICY["windows"]["merchant_response_days"], minutes=1)
    for _ in range(200):
        status = (await one("SELECT status FROM jobs WHERE dispute_id = :d AND kind = 'MERCHANT_DEADLINE'", d=d6))["status"]
        if status == "FAILED":
            break
        await worker_svc.process_one_job(F, broken, clock)
        clock = clock + timedelta(seconds=301)
    failed = await one("SELECT status, attempts FROM jobs WHERE dispute_id = :d AND kind = 'MERCHANT_DEADLINE'", d=d6)
    assert (failed["status"], failed["attempts"]) == ("FAILED", 10), dict(failed)
    told = await one("SELECT count(*) AS n FROM notifications WHERE dispute_id = :d AND recipient_role = 'REVIEWER' "
                     "AND message LIKE 'Timer MERCHANT_DEADLINE failed permanently%'", d=d6)
    assert told["n"] == 1
    assert (await one("SELECT state FROM disputes WHERE dispute_id = :d", d=d6))["state"] == "AWAITING_MERCHANT"
    await engine.dispose()
    print("MORE FLOWS PASSED")


def test_more_flows():
    asyncio.run(main())
