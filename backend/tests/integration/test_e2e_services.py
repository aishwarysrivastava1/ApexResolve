# End-to-end test of the whole service layer on PostgreSQL 16 with the demo seed, a simulated clock and the app role.
import os, io, json, asyncio, tempfile
from datetime import datetime, timedelta, timezone
import asyncpg, pgserver
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = os.path.dirname(os.path.abspath(__file__))
from tests.paths import POLICY_PATH, ROLES_SQL, SCHEMA_SQL, pg_host  # noqa: E402
from app.domain.policy import load_policy                                   # noqa: E402
from app.services.common import Ctx, NotFound, Conflict, Invalid              # noqa: E402
from app.gateways.core import MockCore
from app.gateways.carrier import MockCarrier                       # noqa: E402
from app.services import disputes as disputes_svc, evidence as evidence_svc, actions as actions_svc, worker_jobs as worker_svc, metrics as metrics_svc  # noqa: E402
from app.services import integrity as integrity_svc  # noqa: E402

POLICY = load_policy(POLICY_PATH)
KEY = Ed25519PrivateKey.generate()
KID = "ledger-test-1"
CARRIER = MockCarrier({
    "BLUEDART:AWB10000001": {"status": "DELIVERED", "postcode": "110001"},
    "DELHIVERY:AWB10000002": {"status": "NOT_DELIVERED"},
    "DTDC:AWB10000003": {"status": "DELIVERED", "postcode": "560099"},
    "INDIA_POST:RET20000001": {"status": "DELIVERED", "postcode": "400001"},
})
CTX = Ctx(policy=POLICY, signing_key=KEY, key_id=KID, core=MockCore(), carrier=CARRIER)
ASHA = {"sub": "cm-asha", "role": "cardmember"}
RAVI = {"sub": "cm-ravi", "role": "cardmember"}
ACME = {"sub": "mer-acme", "role": "merchant", "merchant_id": "M_ACME"}
SKY = {"sub": "mer-skyline", "role": "merchant", "merchant_id": "M_SKYLINE"}
BOOKS = {"sub": "mer-pageturner", "role": "merchant", "merchant_id": "M_PAGETURNER"}
NEHA = {"sub": "rev-neha", "role": "reviewer"}

srv = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
srv.psql(open(ROLES_SQL).read())
SOCK = pg_host(srv)
T0 = datetime.now(timezone.utc).replace(microsecond=0)
IST = timezone(timedelta(hours=5, minutes=30))


async def setup():
    raw = await asyncpg.connect(f"postgresql://apex_owner@/apex?host={SOCK}")
    await raw.execute(open(SCHEMA_SQL).read())
    today = T0.astimezone(IST).date()
    await raw.execute("""
      INSERT INTO merchants VALUES ('M_ACME','Acme Electronics','400001'),('M_SKYLINE','Skyline Travels','600001'),
                                   ('M_PAGETURNER','PageTurner Books','700001');
      INSERT INTO merchant_se_numbers VALUES ('1000000001','M_ACME'),('1000000002','M_ACME'),('2000000001','M_SKYLINE'),
                                             ('3000000001','M_PAGETURNER');
      INSERT INTO core_accounts VALUES ('acct_asha_01','cm-asha','OPEN','110001','Asha Verma','371449*****8431'),
                                       ('acct_ravi_01','cm-ravi','OPEN','560001','Ravi Menon','378282*****0005');""")
    rows = [
        ("TXN-C08-OK", "acct_asha_01", "1000000001", "CHARGE", 499900, T0 - timedelta(days=12), "ORD-7781", today - timedelta(days=5), None),
        ("TXN-C08-LOST", "acct_asha_01", "1000000001", "CHARGE", 249900, T0 - timedelta(days=20), "ORD-7790", today - timedelta(days=12), None),
        ("TXN-P08-A", "acct_asha_01", "3000000001", "CHARGE", 129900, T0 - timedelta(days=3), "ORD-5520", None, None),
        ("TXN-P08-B", "acct_asha_01", "3000000001", "CHARGE", 129900, T0 - timedelta(days=3) + timedelta(minutes=7), "ORD-5520", None, None),
        ("TXN-C31", "acct_ravi_01", "1000000002", "CHARGE", 349900, T0 - timedelta(days=15), "ORD-8123", today - timedelta(days=10), None),
        ("TXN-C02-OPEN", "acct_ravi_01", "2000000001", "CHARGE", 250000, T0 - timedelta(days=30), "BK-4410", None, None),
        ("TXN-C02-CREDITED", "acct_ravi_01", "2000000001", "CHARGE", 180000, T0 - timedelta(days=25), "BK-4415", None, None),
        ("TXN-C02-CREDITED-R", "acct_ravi_01", "2000000001", "CREDIT", 180000, T0 - timedelta(days=20), "BK-4415", None, "TXN-C02-CREDITED"),
        ("TXN-OLD", "acct_asha_01", "1000000001", "CHARGE", 99900, T0 - timedelta(days=200), "ORD-6001", None, None),
        ("TXN-BIG", "acct_ravi_01", "2000000001", "CHARGE", 7500000, T0 - timedelta(days=10), "BK-4499", None, None),
    ]
    for r in rows:
        await raw.execute("INSERT INTO core_transactions VALUES ($1,$2,$3,$4,$5,'INR',$6,$7,$8,$9)", *r)
    from cryptography.hazmat.primitives import serialization
    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    await raw.execute("INSERT INTO ledger_keys (key_id, algorithm, public_key_pem) VALUES ($1, 'Ed25519', $2)", KID, pem)
    await raw.close()


ENGINE = None
F = None


async def run(fn, *args):
    """Run one service call in one transaction (like one HTTP request)."""
    async with F() as session:
        async with session.begin():
            return await fn(session, CTX, *args)


async def drain(now):
    """Run the worker until nothing is due at `now`."""
    while await worker_svc.process_one_job(F, CTX, now) or await worker_svc.relay_one_outbox(F, CTX, now):
        pass


async def state_of(dispute_id):
    async with F() as s:
        row = (await s.execute(text("""SELECT d.state, d.status_reason, x.verdict, x.rule_id, x.refund_amount_minor,
            x.margin FROM disputes d LEFT JOIN decisions x ON x.decision_id = d.current_decision_id
            WHERE d.dispute_id = :d"""), {"d": dispute_id})).mappings().one()
        return dict(row)


def jpeg_with_exif():
    img = Image.new("RGB", (40, 30), (200, 10, 10))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"   # Make
    exif[0x8825] = {2: (28.0, 36.0, 0.0)}  # GPS IFD latitude
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


async def expect(exc_type, coro, problem=None):
    try:
        await coro
    except exc_type as e:
        if problem:
            assert e.problem == problem, (e.problem, problem)
        return
    raise AssertionError(f"expected {exc_type.__name__}")


def file_body(txn, code, amount, statement="Item never arrived. My card is 3714 496353 98431, email a@b.co"):
    return {"transaction_id": txn, "reason_code": code, "disputed_amount_minor": amount, "statement": statement}


async def main():
    global ENGINE, F
    await setup()
    ENGINE = create_async_engine(f"postgresql+asyncpg://apex_app@/apex?host={SOCK}", pool_size=20)
    F = async_sessionmaker(ENGINE, expire_on_commit=False)
    now = T0
    results = {}

    # ---- 1. C08 merchant wins (G01-like)
    status, d1, _ = await run(disputes_svc.file_dispute, ASHA, file_body("TXN-C08-OK", "C08", 499900), "idem-000001", now)
    assert (await state_of(d1))["state"] == "AWAITING_MERCHANT"
    async with F() as s:
        note = (await s.execute(text("SELECT note_redacted FROM evidence_items WHERE dispute_id=:d AND seq=1"), {"d": d1})).scalar()
    assert "[REDACTED_CARD]" in note and "[REDACTED_EMAIL]" in note and "3714" not in note, note
    # replay with same key/body returns the same dispute; different body -> 422
    st2, d1b, replayed = await run(disputes_svc.file_dispute, ASHA, file_body("TXN-C08-OK", "C08", 499900), "idem-000001", now)
    assert replayed and d1b == d1
    await expect(Invalid, run(disputes_svc.file_dispute, ASHA, file_body("TXN-C08-OK", "C08", 1000), "idem-000001", now), "idempotency-key-reuse")
    await expect(Conflict, run(disputes_svc.file_dispute, ASHA, file_body("TXN-C08-OK", "C08", 1000), "idem-000002", now), "dispute-exists")
    await expect(NotFound, run(disputes_svc.file_dispute, RAVI, file_body("TXN-C08-LOST", "C08", 1000), "idem-000003", now))
    await expect(NotFound, run(actions_svc.contest, SKY, d1, now))          # other merchant org: not found
    await expect(Invalid, run(actions_svc.contest, ACME, d1, now), "evidence-required")
    await run(evidence_svc.add_evidence, ACME, d1, {"kind": "shipment_tracking", "carrier": "BLUEDART", "tracking_number": "AWB10000001"}, None, now)
    await run(actions_svc.contest, ACME, d1, now)
    await expect(Conflict, run(actions_svc.contest, ACME, d1, now))          # illegal now
    await expect(Invalid, run(evidence_svc.add_evidence, ACME, d1, {"kind": "text", "evidence_type": "merchant_statement", "note": "late"}, None, now), "evidence-not-allowed")
    await run(actions_svc.rebuttal_done, ASHA, d1, now)
    await drain(now)
    r = await state_of(d1)
    assert (r["state"], r["verdict"], r["rule_id"], r["margin"]) == ("DECIDED", "MERCHANT_UPHELD", "R4_MERCHANT_EVIDENCE_STRONGER", "0.855"), r
    results["C08 merchant wins"] = r

    # ---- 2. P08 fast path refund (FP1) -> settles after the appeal window
    _, d2, _ = await run(disputes_svc.file_dispute, ASHA, file_body("TXN-P08-B", "P08", 129900, "Charged twice"), "idem-000004", now)
    r = await state_of(d2)
    assert (r["state"], r["rule_id"], r["refund_amount_minor"]) == ("DECIDED", "FP1_DUPLICATE_CONFIRMED", 129900), r

    # ---- 3. C02 already credited (FP3)
    _, d3, _ = await run(disputes_svc.file_dispute, RAVI, file_body("TXN-C02-CREDITED", "C02", 180000, "Refund promised"), "idem-000005", now)
    assert (await state_of(d3))["rule_id"] == "FP3_CREDIT_ALREADY_POSTED"

    # ---- 4. filing too late (E1)
    _, d4, _ = await run(disputes_svc.file_dispute, ASHA, file_body("TXN-OLD", "C31", 99900, "Wrong colour"), "idem-000006", now)
    r = await state_of(d4)
    assert (r["state"], r["status_reason"]) == ("REJECTED_INELIGIBLE", "E1_FILING_WINDOW_EXPIRED"), r

    # ---- 5. C31 with files -> settlement offer accepted by both (G11)
    _, d5, _ = await run(disputes_svc.file_dispute, RAVI, file_body("TXN-C31", "C31", 349900, "Colour is different from the listing"), "idem-000007", now)
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
    await run(evidence_svc.add_evidence, ACME, d5, {"kind": "file", "evidence_type": "description_match_proof"}, pdf, now)
    await run(evidence_svc.add_evidence, RAVI, d5, {"kind": "file", "evidence_type": "item_photos", "note": "call me on 9876543210"}, jpeg_with_exif(), now)
    await expect(evidence_svc.UploadError, run(evidence_svc.add_evidence, RAVI, d5, {"kind": "file", "evidence_type": "item_photos"}, b"MZ\x90\x00 not an image", now))
    async with F() as s:
        stored = (await s.execute(text("SELECT data FROM evidence_files WHERE uploaded_by='CARDMEMBER' AND dispute_id=:d"), {"d": d5})).scalar()
    assert b"PhoneMaker" not in stored and Image.open(io.BytesIO(stored)).getexif() == {}, "EXIF must be stripped"
    await run(actions_svc.contest, ACME, d5, now)
    await run(actions_svc.rebuttal_done, RAVI, d5, now)
    await drain(now)
    r = await state_of(d5)
    assert r["state"] == "SETTLEMENT_OFFERED", r
    async with F() as s:
        offer = (await s.execute(text("SELECT refund_amount_minor, margin FROM settlement_offers WHERE dispute_id=:d"), {"d": d5})).one()
    assert (offer.refund_amount_minor, offer.margin) == (179516, "-0.0261"), offer
    await run(actions_svc.offer_response, RAVI, d5, "ACCEPTED", now)
    await expect(Conflict, run(actions_svc.offer_response, RAVI, d5, "ACCEPTED", now), "offer-already-answered")
    await run(actions_svc.offer_response, ACME, d5, "ACCEPTED", now)
    r = await state_of(d5)
    assert (r["state"], r["verdict"], r["refund_amount_minor"]) == ("DECIDED", "SPLIT_SETTLEMENT", 179516), r
    await drain(now)
    assert (await state_of(d5))["state"] == "CLOSED"

    # ---- 6. C08 lost -> rebuttal deadline -> R5 refund -> merchant appeals -> reviewer split
    _, d6, _ = await run(disputes_svc.file_dispute, ASHA, file_body("TXN-C08-LOST", "C08", 249900, "Never came"), "idem-000008", now)
    await run(evidence_svc.add_evidence, ACME, d6, {"kind": "shipment_tracking", "carrier": "DELHIVERY", "tracking_number": "AWB10000002"}, None, now)
    # the carrier result supports the CARDMEMBER, so the merchant still has no merchant-side item
    await expect(Invalid, run(actions_svc.contest, ACME, d6, now), "evidence-required")
    await run(evidence_svc.add_evidence, ACME, d6, {"kind": "text", "evidence_type": "merchant_statement", "note": "We handed it to the courier."}, None, now)
    await run(actions_svc.contest, ACME, d6, now)
    later = now + timedelta(days=7, seconds=1)
    await drain(later)
    r = await state_of(d6)
    assert (r["verdict"], r["rule_id"]) == ("CARDMEMBER_REFUND", "R5_CARDMEMBER_EVIDENCE_STRONGER"), r
    await expect(Invalid, run(actions_svc.appeal, ACME, d6, "too short", later))
    await run(actions_svc.appeal, ACME, d6, "We have a signed delivery slip from the courier office.", later)
    await expect(Conflict, run(actions_svc.appeal, ASHA, d6, "I also want to appeal this decision now.", later), "appeal-not-allowed")
    assert (await state_of(d6))["status_reason"] == "APPEAL_BY_MERCHANT"
    async with F() as s:
        stored_reason = (await s.execute(text("SELECT appeal_reason_redacted FROM disputes WHERE dispute_id = :d"),
                                         {"d": d6})).scalar()
    assert stored_reason == "We have a signed delivery slip from the courier office."   # kept for the reviewer
    await run(actions_svc.review_decision, NEHA, d6, "SPLIT_SETTLEMENT", 100000, "Slip is genuine but the address is unclear; splitting.", later)
    await drain(later)
    r = await state_of(d6)
    assert (r["state"], r["verdict"], r["refund_amount_minor"]) == ("CLOSED", "SPLIT_SETTLEMENT", 100000), r

    # ---- 7. C02 merchant silent -> R0 after 20 days -> settles 5 days later
    _, d7, _ = await run(disputes_svc.file_dispute, RAVI, file_body("TXN-C02-OPEN", "C02", 250000, "Cancelled booking, no refund"), "idem-000009", now)
    assert (await state_of(d7))["state"] == "AWAITING_MERCHANT"
    t20 = now + timedelta(days=20, seconds=1)
    await drain(t20)
    r = await state_of(d7)
    assert (r["state"], r["rule_id"]) == ("DECIDED", "R0_MERCHANT_NO_TIMELY_RESPONSE"), r

    # ---- 8. big amount -> R1 human review
    _, d8, _ = await run(disputes_svc.file_dispute, RAVI, file_body("TXN-BIG", "C08", 7500000, "Tickets never issued"), "idem-000010", now)
    await run(evidence_svc.add_evidence, SKY, d8, {"kind": "text", "evidence_type": "merchant_statement", "note": "Tickets were emailed."}, None, now)
    await run(actions_svc.contest, SKY, d8, now)
    await run(actions_svc.rebuttal_done, RAVI, d8, now)
    await drain(now)
    r = await state_of(d8)
    assert (r["state"], r["status_reason"]) == ("HUMAN_REVIEW", "R1_AMOUNT_ABOVE_AUTO_LIMIT"), r

    # ---- advance past every appeal window and settle everything
    t_end = t20 + timedelta(days=6)
    await drain(t_end)
    final = {name: (await state_of(d))["state"] for name, d in
             [("C08 merchant wins", d1), ("P08 fast path", d2), ("C02 credited", d3), ("C31 offer", d5),
              ("C08 appeal", d6), ("C02 silent", d7), ("big", d8), ("old", d4)]}
    print("final states:", final)
    assert final == {"C08 merchant wins": "CLOSED", "P08 fast path": "CLOSED", "C02 credited": "CLOSED",
                     "C31 offer": "CLOSED", "C08 appeal": "CLOSED", "C02 silent": "CLOSED", "big": "HUMAN_REVIEW",
                     "old": "REJECTED_INELIGIBLE"}

    # ---- integrity: transfers, double entry, ledger
    async with F() as s:
        transfers = (await s.execute(text("SELECT dispute_id, amount_minor FROM core_transfers ORDER BY amount_minor"))).all()
        integrity_problems = await integrity_svc.check(s)      # APX-06 §7: double entry, paid once, timers …
        ok, msg = await worker_svc.verify_ledger(s, {KID: KEY.public_key()})
        payloads = (await s.execute(text("SELECT payload_canonical FROM audit_events"))).scalars().all()
    amounts = sorted(t.amount_minor for t in transfers)
    print("transfers:", amounts)
    assert amounts == [100000, 129900, 179516, 250000], amounts
    assert integrity_problems == [], integrity_problems
    assert ok, msg
    assert not any("3714" in p or "REDACTED" in p or "@" in p for p in payloads), "ledger must not contain text"
    # relay twice for the same key never posts again
    async with F() as s:
        async with s.begin():
            key = (await s.execute(text("SELECT idempotency_key, dispute_id, payload FROM outbox LIMIT 1"))).one()
            await CTX.core.transfer(s, key.idempotency_key, key.dispute_id, key.payload["se_number"], key.payload["account_token"], key.payload["amount_minor"], "INR")
        n = (await s.execute(text("SELECT count(*) FROM core_transfers"))).scalar()
    assert n == 4
    # checkpoint + verify against it
    async with F() as s:
        async with s.begin():
            cp = await worker_svc.create_checkpoint(s, CTX, t_end)
        ok2, msg2 = await worker_svc.verify_ledger(s, {KID: KEY.public_key()}, cp)
    assert ok2, msg2
    async with F() as s:
        m = await metrics_svc.summary(s, (T0 - timedelta(days=1)).date(), (t_end + timedelta(days=1)).date())
    print("ledger:", msg, "| checkpoint:", msg2)
    print("metrics:", json.dumps(m, default=str))
    # T-INT-METRIC-01: 6 closed = R4, FP1, FP3, offer accepted (all SYSTEM), reviewer split after appeal, R0
    rates = {k: m[k] for k in ("closed", "automation_rate", "fast_path_rate", "offer_rate", "offer_acceptance_rate",
                               "human_review_rate", "appeal_count")}
    assert rates == {"closed": 6, "automation_rate": "0.8333", "fast_path_rate": "0.3333", "offer_rate": "0.1667",
                     "offer_acceptance_rate": "1.0000", "human_review_rate": "0.1667", "appeal_count": 1}, rates
    print("E2E PASSED")


def test_main():
    asyncio.run(main())
