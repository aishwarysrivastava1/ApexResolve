# The demo scenarios of APX-14 §3.2, run on the REAL demo seed through the service layer, so the demo guide can
# never promise an outcome the engine does not produce. Each block is one scenario (S1…S7).
import asyncio
import json
import os
import subprocess  # noqa: S404 (fixed commands only)
import sys
import tempfile
from datetime import datetime, timezone

import pgserver
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.paths import BACKEND, POLICY_PATH, ROLES_SQL, SEED_JSON, pg_host
from app.bootstrap import register_public_key_and_load_all
from app.domain.policy import load_policy
from app.gateways.carrier import MockCarrier
from app.gateways.core import MockCore
from app.services import actions, disputes, evidence, worker_jobs
from app.services.common import Ctx, Invalid

KEY = Ed25519PrivateKey.generate()
CTX = Ctx(policy=load_policy(POLICY_PATH), signing_key=KEY, key_id="k-demo", core=MockCore(),
          carrier=MockCarrier(json.load(open(SEED_JSON))["carrier_fixtures"]))
ASHA = {"sub": "cm-asha", "role": "cardmember"}
RAVI = {"sub": "cm-ravi", "role": "cardmember"}
ACME = {"sub": "mer-acme", "role": "merchant", "merchant_id": "M_ACME"}
SKYLINE = {"sub": "mer-skyline", "role": "merchant", "merchant_id": "M_SKYLINE"}
F = None
NOW = None
TINY_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


async def run(fn, *args):
    async with F() as session, session.begin():
        return await fn(session, CTX, *args)


async def file(principal, txn, code, key):
    async with F() as session:
        amount = (await session.execute(text("SELECT amount_minor FROM core_transactions WHERE transaction_id = :t"),
                                        {"t": txn})).scalar()
    body = {"transaction_id": txn, "reason_code": code, "disputed_amount_minor": amount, "statement": "Demo statement."}
    _status, dispute_id, _replayed = await run(disputes.file_dispute, principal, body, key, NOW)
    return dispute_id


async def outcome(dispute_id):
    async with F() as session:
        row = (await session.execute(text(
            "SELECT d.state, d.status_reason, x.rule_id, x.verdict, x.refund_amount_minor, o.refund_amount_minor AS offer "
            "FROM disputes d LEFT JOIN decisions x ON x.decision_id = d.current_decision_id "
            "LEFT JOIN settlement_offers o ON o.dispute_id = d.dispute_id WHERE d.dispute_id = :d"),
            {"d": dispute_id})).mappings().one()
    return dict(row)


async def contest_and_finish(merchant, cardmember, dispute_id):
    await run(actions.contest, merchant, dispute_id, NOW)
    await run(actions.rebuttal_done, cardmember, dispute_id, NOW)
    while await worker_jobs.process_one_job(F, CTX, NOW):
        pass


def statement(note):
    return {"kind": "text", "evidence_type": "merchant_statement", "note": note}


def tracking(kind, carrier, number):
    return {"kind": kind, "carrier": carrier, "tracking_number": number}


async def main():
    global F, NOW
    server = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    server.psql(open(ROLES_SQL).read())
    sock = pg_host(server)
    owner = f"postgresql://apex_owner@/apex?host={sock}"
    env = dict(os.environ, MIGRATION_DATABASE_URL=owner, APP_ENV="demo")
    subprocess.run([sys.executable, "scripts/migrate.py", "migrations"], cwd=str(BACKEND), env=env, check=True)  # noqa: S603
    subprocess.run([sys.executable, "scripts/seed_demo.py", SEED_JSON], cwd=str(BACKEND), env=env, check=True)  # noqa: S603
    engine = create_async_engine(f"postgresql+asyncpg://apex_app@/apex?host={sock}")
    F = async_sessionmaker(engine, expire_on_commit=False)
    await register_public_key_and_load_all(F, CTX)
    NOW = datetime.now(timezone.utc)
    results = {}

    # S1: verified delivery beats a statement → R4
    s1 = await file(ASHA, "TXN-C08-OK", "C08", "demo-s1-key")
    await run(evidence.add_evidence, ACME, s1, tracking("shipment_tracking", "BLUEDART", "AWB10000001"), None, NOW)
    await contest_and_finish(ACME, ASHA, s1)
    results["S1"] = await outcome(s1)
    # S2: duplicate charge → FP1 refund at filing
    results["S2"] = await outcome(await file(ASHA, "TXN-P08-B", "P08", "demo-s2-key"))
    # S3: carrier says not delivered; the merchant needs its own item to contest → R5
    s3 = await file(ASHA, "TXN-C08-LOST", "C08", "demo-s3-key")
    await run(evidence.add_evidence, ACME, s3, tracking("shipment_tracking", "DELHIVERY", "AWB10000002"), None, NOW)
    try:
        await run(actions.contest, ACME, s3, NOW)
        results["S3 contest without own evidence"] = "allowed"
    except Invalid as refused:
        results["S3 contest without own evidence"] = refused.problem
    await run(evidence.add_evidence, ACME, s3, statement("We shipped it."), None, NOW)
    await contest_and_finish(ACME, ASHA, s3)
    results["S3"] = await outcome(s3)
    # S5 (pre-staged in the demo): high value → R1 human review
    s5 = await file(RAVI, "TXN-BIG", "C02", "demo-s5-key")
    await run(evidence.add_evidence, SKYLINE, s5, statement("Refund was never promised."), None, NOW)
    await contest_and_finish(SKYLINE, RAVI, s5)
    results["S5"] = await outcome(s5)
    # S4: listing proof vs photos → R6 offer of ₹1,795.16
    s4 = await file(RAVI, "TXN-C31", "C31", "demo-s4-key")
    await run(evidence.add_evidence, RAVI, s4, {"kind": "file", "evidence_type": "item_photos"}, TINY_PDF, NOW)
    await run(evidence.add_evidence, ACME, s4, {"kind": "file", "evidence_type": "description_match_proof"}, TINY_PDF, NOW)
    await contest_and_finish(ACME, RAVI, s4)
    results["S4"] = await outcome(s4)
    # S6: already credited → FP3, nothing to refund
    results["S6"] = await outcome(await file(RAVI, "TXN-C02-CREDITED", "C02", "demo-s6-key"))
    # S7: 150 days old → E1
    results["S7"] = await outcome(await file(ASHA, "TXN-OLD", "C31", "demo-s7-key"))
    await engine.dispose()
    return results


async def main_s4b():
    """S4b on a fresh database: the same C31 case plus a verified return shipment → R5 refund."""
    global F, NOW
    server = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    server.psql(open(ROLES_SQL).read())
    sock = pg_host(server)
    env = dict(os.environ, MIGRATION_DATABASE_URL=f"postgresql://apex_owner@/apex?host={sock}", APP_ENV="demo")
    subprocess.run([sys.executable, "scripts/migrate.py", "migrations"], cwd=str(BACKEND), env=env, check=True)  # noqa: S603
    subprocess.run([sys.executable, "scripts/seed_demo.py", SEED_JSON], cwd=str(BACKEND), env=env, check=True)  # noqa: S603
    engine = create_async_engine(f"postgresql+asyncpg://apex_app@/apex?host={sock}")
    F = async_sessionmaker(engine, expire_on_commit=False)
    await register_public_key_and_load_all(F, CTX)
    NOW = datetime.now(timezone.utc)
    s4 = await file(RAVI, "TXN-C31", "C31", "demo-s4b-key")
    await run(evidence.add_evidence, RAVI, s4, {"kind": "file", "evidence_type": "item_photos"}, TINY_PDF, NOW)
    await run(evidence.add_evidence, RAVI, s4, tracking("return_tracking", "INDIA_POST", "RET20000001"), None, NOW)
    await run(evidence.add_evidence, ACME, s4, {"kind": "file", "evidence_type": "description_match_proof"}, TINY_PDF, NOW)
    await contest_and_finish(ACME, RAVI, s4)
    result = await outcome(s4)
    await engine.dispose()
    return result


def test_demo_scenarios_match_the_demo_guide():
    r = asyncio.run(main())
    assert (r["S1"]["rule_id"], r["S1"]["verdict"]) == ("R4_MERCHANT_EVIDENCE_STRONGER", "MERCHANT_UPHELD"), r["S1"]
    assert (r["S2"]["state"], r["S2"]["rule_id"], r["S2"]["refund_amount_minor"]) == ("DECIDED", "FP1_DUPLICATE_CONFIRMED", 129900)
    assert r["S3 contest without own evidence"] == "evidence-required"
    assert (r["S3"]["rule_id"], r["S3"]["refund_amount_minor"]) == ("R5_CARDMEMBER_EVIDENCE_STRONGER", 249900), r["S3"]
    assert (r["S4"]["state"], r["S4"]["offer"]) == ("SETTLEMENT_OFFERED", 179516), r["S4"]
    assert (r["S5"]["state"], r["S5"]["status_reason"]) == ("HUMAN_REVIEW", "R1_AMOUNT_ABOVE_AUTO_LIMIT"), r["S5"]
    assert (r["S6"]["rule_id"], r["S6"]["refund_amount_minor"]) == ("FP3_CREDIT_ALREADY_POSTED", 0), r["S6"]
    assert (r["S7"]["state"], r["S7"]["status_reason"]) == ("REJECTED_INELIGIBLE", "E1_FILING_WINDOW_EXPIRED"), r["S7"]


def test_demo_scenario_s4b_verified_return_tips_the_balance():
    r = asyncio.run(main_s4b())
    assert (r["rule_id"], r["refund_amount_minor"]) == ("R5_CARDMEMBER_EVIDENCE_STRONGER", 349900), r
