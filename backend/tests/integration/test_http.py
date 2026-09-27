# HTTP-level test: dev IdP -> JWT -> API -> services -> PostgreSQL 16, plus the worker loop.
import os, json, asyncio, tempfile, uuid
from datetime import datetime, timedelta, timezone
import asyncpg, pgserver, httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = os.path.dirname(os.path.abspath(__file__))
from tests.paths import POLICY_PATH, ROLES_SQL, SCHEMA_SQL, pg_host  # noqa: E402
from app.settings import ApiSettings            # noqa: E402
from app.main import create_app              # noqa: E402
from devidp.main import create_devidp            # noqa: E402
from app import worker as worker_main                               # noqa: E402

TMP = tempfile.mkdtemp()
srv = pgserver.get_server(os.path.join(TMP, "pg"), cleanup_mode="stop")
srv.psql(open(ROLES_SQL).read())
SOCK = pg_host(srv)
T0 = datetime.now(timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))

# secrets and fixtures on disk, like the containers see them
KEY_PATH = os.path.join(TMP, "ledger.pem")
with open(KEY_PATH, "wb") as f:
    f.write(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
CARRIER_PATH = os.path.join(TMP, "carrier.json")
json.dump({"BLUEDART:AWB10000001": {"status": "DELIVERED", "postcode": "110001"}}, open(CARRIER_PATH, "w"))
USERS = {"cm-asha": {"role": "cardmember", "name": "Asha Verma"},
         "cm-ravi": {"role": "cardmember", "name": "Ravi Menon"},
         "cm-mira": {"role": "cardmember", "name": "Mira Das"},
         "mer-acme": {"role": "merchant", "name": "Acme staff", "merchant_id": "M_ACME"},
         "mer-skyline": {"role": "merchant", "name": "Skyline staff", "merchant_id": "M_SKYLINE"},
         "rev-neha": {"role": "reviewer", "name": "Neha"},
         "aud-kabir": {"role": "auditor", "name": "Kabir"}}
PASSWORD = "demo-password-123"


async def seed():
    raw = await asyncpg.connect(f"postgresql://apex_owner@/apex?host={SOCK}")
    await raw.execute(open(SCHEMA_SQL).read())
    await raw.execute("""
      INSERT INTO merchants VALUES ('M_ACME','Acme Electronics','400001'),('M_SKYLINE','Skyline Travels','600001');
      INSERT INTO merchant_se_numbers VALUES ('1000000001','M_ACME'),('2000000001','M_SKYLINE');
      INSERT INTO core_accounts VALUES ('acct_asha_01','cm-asha','OPEN','110001','Asha Verma','371449*****8431');""")
    await raw.execute("INSERT INTO core_transactions VALUES ('TXN-C08-OK','acct_asha_01','1000000001','CHARGE',499900,'INR',$1,'ORD-7781',$2,NULL)",
                      T0 - timedelta(days=12), (T0.astimezone(IST) - timedelta(days=5)).date())
    # Ravi has 11 old charges (outside the filing window) to exercise the daily filing quota (T-SEC-21)
    await raw.execute("INSERT INTO core_accounts VALUES ('acct_ravi_01','cm-ravi','OPEN','560001','Ravi Menon','378282*****0005')")
    for i in range(11):
        await raw.execute("INSERT INTO core_transactions VALUES ($1,'acct_ravi_01','1000000001','CHARGE',10000,'INR',$2,NULL,NULL,NULL)",
                          f"TXN-OLD-{i:02d}", T0 - timedelta(days=200))
    # Mira has two high-value charges (above the automation limit) for the review-queue checks (T-INT-REV-01)
    await raw.execute("INSERT INTO core_accounts VALUES ('acct_mira_01','cm-mira','OPEN','700001','Mira Das','371449*****0001')")
    for i in (1, 2):
        await raw.execute("INSERT INTO core_transactions VALUES ($1,'acct_mira_01','1000000001','CHARGE',6000000,'INR',$2,NULL,$3,NULL)",
                          f"TXN-BIG-{i}", T0 - timedelta(days=5, minutes=i), (T0.astimezone(IST) - timedelta(days=1)).date())
    await raw.close()


async def main():
    await seed()
    settings = ApiSettings(app_env="test", database_url=f"postgresql+asyncpg://apex_app@/apex?host={SOCK}",
                        policy_path=POLICY_PATH,
                        ledger_signing_key_path=KEY_PATH, ledger_key_id="ledger-test", oidc_issuer="apexresolve-devidp",
                        oidc_jwks_url="http://devidp/jwks.json", carrier_fixtures_path=CARRIER_PATH)
    idp = create_devidp("test", "apexresolve-devidp", "apexresolve-api", USERS, PASSWORD)
    idp_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp), base_url="http://devidp")
    api = create_app(settings, jwks_http_client=idp_client)
    checks = {}
    async with api.router.lifespan_context(api):
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=api, raise_app_exceptions=False), base_url="http://api")

        async def login(user):
            r = await idp_client.post("/token", json={"username": user, "password": PASSWORD})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        A, M, SKY, REV, AUD = [await login(u) for u in ["cm-asha", "mer-acme", "mer-skyline", "rev-neha", "aud-kabir"]]
        checks["wrong password"] = (await idp_client.post("/token", json={"username": "cm-asha", "password": "x" * 12})).status_code
        body = {"transaction_id": "TXN-C08-OK", "reason_code": "C08", "disputed_amount_minor": 499900,
                "statement": "Never arrived; card 371449635398431"}
        checks["no idempotency key"] = (await c.post("/api/v1/disputes", json=body, headers=A)).status_code
        short = await c.post("/api/v1/disputes", json=body, headers=A | {"Idempotency-Key": "short"})
        checks["short idempotency key"] = (short.status_code, short.json()["type"])
        checks["forged v1 token"] = (await c.post("/api/v1/disputes", json=body, headers={"Authorization": "Bearer cm_token_cm-asha", "Idempotency-Key": "k" * 10})).status_code
        checks["merchant files dispute"] = (await c.post("/api/v1/disputes", json=body, headers=M | {"Idempotency-Key": "k" * 10})).status_code
        bad = await c.post("/api/v1/disputes", json=body | {"disputed_amount_minor": "371449635398431x"}, headers=A | {"Idempotency-Key": "k" * 10})
        checks["422 does not echo input"] = (bad.status_code, "371449635398431" in bad.text)
        checks["unknown field rejected"] = (await c.post("/api/v1/disputes", json=body | {"days_since_transaction": 5}, headers=A | {"Idempotency-Key": "k" * 10})).status_code
        r = await c.post("/api/v1/disputes", json=body, headers=A | {"Idempotency-Key": "file-0001"})
        dispute = r.json()
        did = dispute["dispute_id"]
        checks["file"] = (r.status_code, dispute["state"], dispute["allowed_actions"])
        checks["statement redacted"] = dispute["evidence"][0]["note"]
        checks["headers"] = (r.headers["x-content-type-options"], r.headers["cache-control"], "x-request-id" in r.headers,
                              r.headers["referrer-policy"], "frame-ancestors 'none'" in r.headers["content-security-policy"])
        r2 = await c.post("/api/v1/disputes", json=body, headers=A | {"Idempotency-Key": "file-0001"})
        checks["replay"] = (r2.status_code, r2.json()["dispute_id"] == did)
        checks["other merchant sees 404"] = (await c.get(f"/api/v1/disputes/{did}", headers=SKY)).status_code
        checks["merchant view has no risk flag"] = "risk_flag" in (await c.get(f"/api/v1/disputes/{did}", headers=M)).json()
        # the card mask is visible to the cardmember and reviewers only (APX-07 §2.1)
        checks["card mask by role"] = [("card_display_mask" in (await c.get(f"/api/v1/disputes/{did}", headers=h)).json()["transaction"])
                                       for h in (A, M, REV, AUD)]
        r = await c.post(f"/api/v1/disputes/{did}/evidence", headers=M,
                         data={"kind": "shipment_tracking", "carrier": "BLUEDART", "tracking_number": "AWB10000001"})
        checks["tracking"] = (r.status_code, [(e["evidence_type"], e["source"]) for e in r.json()["evidence"]][-1])
        r = await c.post(f"/api/v1/disputes/{did}/evidence", headers=M, data={"kind": "file", "evidence_type": "signed_proof_of_delivery"},
                         files={"file": ("pod.pdf", b"%PDF-1.4 minimal", "application/pdf")})
        checks["pdf upload"] = r.status_code
        exe = await c.post(f"/api/v1/disputes/{did}/evidence", headers=M, data={"kind": "file", "evidence_type": "signed_proof_of_delivery"},
                           files={"file": ("pod.pdf", b"MZ\x90 fake pdf", "application/pdf")})
        checks["disguised exe"] = (exe.status_code, exe.json()["type"])
        file_id = [e["file_id"] for e in r.json()["evidence"] if e["file_id"]][0]
        dl = await c.get(f"/api/v1/disputes/{did}/files/{file_id}", headers=A)
        checks["download"] = (dl.status_code, dl.headers["content-disposition"].startswith("attachment"))
        checks["auditor cannot download"] = (await c.get(f"/api/v1/disputes/{did}/files/{file_id}", headers=AUD)).status_code
        checks["cardmember cannot contest"] = (await c.post(f"/api/v1/disputes/{did}/contest", headers=A)).status_code
        checks["contest"] = (await c.post(f"/api/v1/disputes/{did}/contest", headers=M)).json()["state"]
        checks["contest again"] = (await c.post(f"/api/v1/disputes/{did}/contest", headers=M)).status_code
        checks["rebuttal-done"] = (await c.post(f"/api/v1/disputes/{did}/rebuttal-done", headers=A)).json()["state"]
        await worker_main.run_worker(api.state.sessions, api.state.ctx, max_loops=1)
        view = (await c.get(f"/api/v1/disputes/{did}", headers=A)).json()
        checks["decision"] = (view["state"], view["decision"]["verdict"], view["decision"]["rule_id"], view["decision"]["margin"], view["allowed_actions"])
        checks["explanation"] = view["decision"]["explanation"]
        checks["auditor verify"] = (await c.post("/api/v1/audit/verify", headers=AUD)).json()
        checks["reviewer cannot verify"] = (await c.post("/api/v1/audit/verify", headers=REV)).status_code
        checks["readyz"] = (await c.get("/readyz")).json()
        checks["my transactions"] = [(t["transaction_id"], t["dispute_id"] == did) for t in (await c.get("/api/v1/me/transactions", headers=A)).json()["items"]]
        checks["merchant list"] = [d["dispute_id"] == did for d in (await c.get("/api/v1/disputes", headers=M)).json()["items"]]
        checks["skyline list empty"] = (await c.get("/api/v1/disputes", headers=SKY)).json()["items"]
        checks["timeline"] = [e["event_type"] for e in (await c.get(f"/api/v1/disputes/{did}/timeline", headers=A)).json()["items"]]
        # T-INT-TIMELINE-01: parties see only the actor's role; reviewers also see who acted
        party_items = (await c.get(f"/api/v1/disputes/{did}/timeline", headers=A)).json()["items"]
        staff_items = (await c.get(f"/api/v1/disputes/{did}/timeline", headers=REV)).json()["items"]
        checks["timeline actor detail"] = (any("actor" in e for e in party_items),
                                           all("actor" in e for e in staff_items))
        cp = (await c.post("/api/v1/audit/checkpoints", headers=AUD)).json()
        checks["verify with checkpoint"] = (await c.post("/api/v1/audit/verify", headers=AUD, content=json.dumps(cp))).json()
        checks["verify malformed"] = [(await c.post("/api/v1/audit/verify", headers=AUD, content=body)).status_code
                                      for body in ("{not json", "[1,2]", json.dumps({"seq": "1"}))]
        # T-INT-REV-01 through the API: two high-value cases reach the review queue, oldest first, with their reason
        MIRA = await login("cm-mira")
        big = []
        for i in (1, 2):
            r = await c.post("/api/v1/disputes", headers=MIRA | {"Idempotency-Key": f"mira-big-{i}"},
                             json={"transaction_id": f"TXN-BIG-{i}", "reason_code": "C08",
                                   "disputed_amount_minor": 6000000, "statement": "Never delivered."})
            big.append(r.json()["dispute_id"])
            # T-INT-EVID-07: the client cannot choose its evidence's source (an extra "source" field is ignored)
            ev = await c.post(f"/api/v1/disputes/{big[-1]}/evidence", headers=M,
                              data={"kind": "text", "evidence_type": "merchant_statement", "note": "Delivered.",
                                    "source": "system_verified"})
            await c.post(f"/api/v1/disputes/{big[-1]}/contest", headers=M)
            await c.post(f"/api/v1/disputes/{big[-1]}/rebuttal-done", headers=MIRA)
        checks["client-chosen source ignored"] = ev.json()["evidence"][-1]["source"]
        await worker_main.run_worker(api.state.sessions, api.state.ctx, max_loops=1)
        queue = (await c.get("/api/v1/review/queue", headers=REV)).json()["items"]
        checks["review queue"] = [(q["dispute_id"], q["status_reason"]) for q in queue] == [
            (big[0], "R1_AMOUNT_ABOVE_AUTO_LIMIT"), (big[1], "R1_AMOUNT_ABOVE_AUTO_LIMIT")]
        checks["R1 explanation"] = (await c.get(f"/api/v1/disputes/{big[0]}", headers=MIRA)).json()["status_explanation"]
        checks["auditor inbox"] = ((await c.get("/api/v1/notifications", headers=AUD)).json()["items"],
                                   (await c.post(f"/api/v1/notifications/{uuid.uuid4()}/read", headers=AUD)).status_code)
        # T-SEC-21: daily filing quota (10 per rolling 24 h), then the per-minute request limit
        R = await login("cm-ravi")
        quota = []
        for i in range(11):
            r = await c.post("/api/v1/disputes", headers=R | {"Idempotency-Key": f"ravi-old-{i:02d}"},
                             json={"transaction_id": f"TXN-OLD-{i:02d}", "reason_code": "C31",
                                   "disputed_amount_minor": 10000, "statement": "not as described"})
            quota.append((r.status_code, r.headers.get("retry-after"), r.json().get("state") or r.json()["type"]))
            if i == 0:
                checks["E1 explanation"] = r.json()["status_explanation"]
        checks["filing quota"] = quota
        burst = [await c.get("/api/v1/policy", headers=SKY) for _ in range(121)]
        checks["per-minute limit"] = (burst[-1].status_code, burst[-1].headers.get("retry-after"), burst[-1].json()["type"])
        checks["notifications (merchant)"] = len((await c.get("/api/v1/notifications", headers=M)).json()["items"])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        checks["metrics"] = (await c.get(f"/api/v1/metrics/summary?from={today}&to={today}", headers=REV)).json()["counts_by_state"]
        checks["policy"] = (await c.get("/api/v1/policy", headers=A)).json()["policy_version"]
    for k, v in checks.items():
        print(f"{k:32} {v}")
    assert checks["wrong password"] == 401 and checks["no idempotency key"] == 400 and checks["forged v1 token"] == 401
    assert checks["merchant files dispute"] == 403 and checks["422 does not echo input"] == (422, False)
    assert checks["unknown field rejected"] == 422 and checks["file"][0:2] == (201, "AWAITING_MERCHANT")
    assert "[REDACTED_CARD]" in checks["statement redacted"] and checks["replay"] == (201, True)
    assert checks["other merchant sees 404"] == 404 and checks["merchant view has no risk flag"] is False
    assert checks["card mask by role"] == [True, False, True, False], checks["card mask by role"]
    assert checks["tracking"] == (201, ("carrier_delivery_confirmation", "system_verified"))
    assert checks["pdf upload"] == 201 and checks["disguised exe"] == (415, "urn:apexresolve:problem:unsupported-file-type")
    assert checks["download"] == (200, True) and checks["auditor cannot download"] == 403
    assert checks["cardmember cannot contest"] == 403 and checks["contest"] == "AWAITING_CARDMEMBER_REBUTTAL"
    assert checks["contest again"] == 409 and checks["rebuttal-done"] == "READY_FOR_DECISION"
    assert checks["decision"][:3] == ("DECIDED", "MERCHANT_UPHELD", "R4_MERCHANT_EVIDENCE_STRONGER")
    assert checks["auditor verify"]["ok"] and checks["reviewer cannot verify"] == 403
    assert checks["my transactions"] == [("TXN-C08-OK", True)] and checks["merchant list"] == [True]
    assert checks["skyline list empty"] == [] and checks["timeline"][0] == "DISPUTE_CREATED"
    assert checks["verify with checkpoint"]["ok"] and checks["policy"] == "2026.09.1"
    assert checks["verify malformed"] == [422, 422, 422], checks["verify malformed"]
    assert checks["filing quota"][:10] == [(201, None, "REJECTED_INELIGIBLE")] * 10, checks["filing quota"]
    assert checks["filing quota"][10] == (429, "3600", "urn:apexresolve:problem:rate-limited"), checks["filing quota"]
    assert checks["per-minute limit"] == (429, "60", "urn:apexresolve:problem:rate-limited"), checks["per-minute limit"]
    assert checks["headers"] == ("nosniff", "no-store", True, "no-referrer", True), checks["headers"]
    assert checks["readyz"] == {"status": "ready", "policy_version": "2026.09.1"}
    assert checks["notifications (merchant)"] >= 1 and checks["review queue"] is True
    assert checks["client-chosen source ignored"] == "self_attested"
    assert checks["timeline actor detail"] == (False, True)
    assert checks["R1 explanation"] == "Disputes above ₹50,000.00 are always decided by a person."
    assert checks["E1 explanation"].startswith("This claim was received after the last filing day (")
    assert checks["auditor inbox"] == ([], 204)
    assert checks["short idempotency key"] == (422, "urn:apexresolve:problem:idempotency-key-invalid")
    assert checks["metrics"].get("DECIDED") == 1 and "merchant 95.6% vs cardmember 4.5%" in checks["explanation"]
    print("HTTP TEST PASSED")


def test_main(capsys):
    asyncio.run(main())
    # T-SEC-22: JSON access logs with request id, route template, status and duration; no bodies, tokens or PANs
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    requests = [line for line in lines if line["event"] == "request"]
    assert len(requests) > 100 and all({"request_id", "route", "status", "duration_ms"} <= set(r) for r in requests)
    text = json.dumps(lines)
    for secret in ("Bearer", "eyJ", "371449635398431", "Never arrived", "cm-asha", "TXN-"):
        assert secret not in text, secret
    assert any(r["route"] == "/api/v1/disputes/{dispute_id}" for r in requests)
