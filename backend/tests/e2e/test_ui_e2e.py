# Browser end-to-end tests T-E2E-01…06: real processes (dev IdP, api, worker, Vite dev server)
# on an embedded PostgreSQL, driven through Chromium with Playwright.
#
# Skipped unless E2E=1 (needs Node 22 with `npm ci` done in web/, `pip install playwright uvicorn`,
# and a Chromium; set CHROMIUM_PATH if Playwright's own browser is not installed).
#   E2E=1 python -m pytest -q tests/e2e
import json
import os
import subprocess  # noqa: S404 (fixed commands only)
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

from tests.paths import BACKEND, REPO, ROLES_SQL, SEED_JSON, pg_host

pytestmark = pytest.mark.skipif(os.environ.get("E2E") != "1", reason="set E2E=1 to run the browser tests")

PASSWORD = "e2e-demo-password"
WEB = REPO / "web"
SHOTS = os.path.join(tempfile.gettempdir(), "apexresolve-e2e")   # screenshots for the demo deck
ERRORS = []                                                       # uncaught browser errors


def wait_for(url, seconds=60):
    # poll until the server answers (any HTTP status counts as "up")
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)  # noqa: S310 (localhost only)
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not start")


def start(name, args, env, cwd):
    # each process logs to <tmp>/apexresolve-e2e/<name>.log for debugging a failed run
    log = open(os.path.join(SHOTS, f"{name}.log"), "w")
    return subprocess.Popen(args, cwd=str(cwd), env=dict(os.environ, **env),  # noqa: S603 (fixed argv)
                            stdout=log, stderr=subprocess.STDOUT)


def sign_in(page, username):
    page.goto("http://127.0.0.1:5173/")
    page.get_by_label("Demo user").select_option(username)
    page.get_by_label("Demo password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.get_by_role("button", name="Sign out").wait_for()


def sign_out(page):
    page.get_by_role("button", name="Sign out").click()
    page.get_by_role("button", name="Sign in").wait_for()


@pytest.fixture(scope="module")
def system():
    import pgserver
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    tmp = tempfile.mkdtemp()
    os.makedirs(SHOTS, exist_ok=True)
    # 1. database: roles, schema (the real migration runner) and the demo seed
    server = pgserver.get_server(os.path.join(tmp, "pg"), cleanup_mode="stop")
    server.psql(open(ROLES_SQL).read())
    sock = pg_host(server)
    owner = f"postgresql://apex_owner@/apex?host={sock}"
    py = sys.executable
    subprocess.run([py, "scripts/migrate.py", "migrations"], cwd=str(BACKEND), check=True,  # noqa: S603
                   env=dict(os.environ, MIGRATION_DATABASE_URL=owner))
    subprocess.run([py, "scripts/seed_demo.py", SEED_JSON], cwd=str(BACKEND), check=True,  # noqa: S603
                   env=dict(os.environ, MIGRATION_DATABASE_URL=owner, APP_ENV="demo"))
    # 2. ledger key and shared settings, exactly as the containers receive them
    key_path = os.path.join(tmp, "ledger.pem")
    with open(key_path, "wb") as handle:
        handle.write(Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    users_path = os.path.join(tmp, "users.json")
    json.dump(json.load(open(SEED_JSON))["devidp_users"], open(users_path, "w"))
    common = {"APP_ENV": "demo", "DATABASE_URL": f"postgresql+asyncpg://apex_app@/apex?host={sock}",
              "POLICY_PATH": str(REPO / "policy" / "policy.2026.09.1.yaml"), "LEDGER_SIGNING_KEY_PATH": key_path,
              "LEDGER_KEY_ID": "ledger-e2e", "CARRIER_FIXTURES_PATH": "fixtures/carrier.json",
              "OIDC_ISSUER": "apexresolve-devidp", "OIDC_AUDIENCE": "apexresolve-api",
              "OIDC_JWKS_URL": "http://127.0.0.1:9000/jwks.json",
              "WORKER_HEARTBEAT_PATH": os.path.join(tmp, "heartbeat")}
    # 3. processes: dev IdP, api, worker, Vite dev server
    processes = [
        start("devidp", [py, "-m", "uvicorn", "devidp.main:app_from_env", "--factory", "--port", "9000"],
              {"APP_ENV": "demo", "DEVIDP_ISSUER": "apexresolve-devidp", "DEVIDP_USERS_PATH": users_path,
               "DEVIDP_DEMO_PASSWORD": PASSWORD}, BACKEND),
    ]
    wait_for("http://127.0.0.1:9000/jwks.json")
    processes.append(start("api", [py, "-m", "uvicorn", "app.main:app_from_env", "--factory", "--port", "8000"],
                           common, BACKEND))
    processes.append(start("worker", [py, "-m", "app.worker"], common, BACKEND))
    wait_for("http://127.0.0.1:8000/readyz")
    # Vite's own launcher through node (npx is a .cmd wrapper on Windows that Popen cannot start)
    processes.append(start("vite", ["node", "node_modules/vite/bin/vite.js", "--port", "5173", "--strictPort",
                                    "--host", "127.0.0.1"],
                           {"APEX_API_URL": "http://127.0.0.1:8000", "APEX_IDP_URL": "http://127.0.0.1:9000"}, WEB))
    wait_for("http://127.0.0.1:5173/")
    yield
    for process in processes:
        process.terminate()


@pytest.fixture(scope="module")
def page(system):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: ERRORS.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())      # the "are you sure?" confirmations
        yield page
        browser.close()


def test_e2e_01_cardmember_files_a_dispute(page):
    sign_in(page, "cm-asha")
    page.get_by_role("row", name="ORD-7781").get_by_role("button", name="Dispute").click()
    page.get_by_label("What happened?").fill("Order ORD-7781 never arrived. My card is 371449635398431.")
    page.get_by_role("button", name="File dispute").click()
    page.get_by_text("Waiting for the merchant").first.wait_for()
    # the card number typed in the statement was redacted before storage
    assert "371449635398431" not in page.content()
    page.screenshot(path=os.path.join(SHOTS, "e2e-01-filed.png"), full_page=True)
    sign_out(page)


def test_e2e_02_merchant_adds_tracking_and_contests(page):
    sign_in(page, "mer-acme")
    page.get_by_role("row", name="C08").first.click()
    page.get_by_label("Evidence type").select_option(label="Carrier tracking number (checked with the carrier)")
    page.get_by_label("Carrier", exact=True).select_option("BLUEDART")
    page.get_by_label("Tracking number", exact=True).fill("AWB10000001")
    page.get_by_role("button", name="Add evidence").click()
    page.get_by_text("Carrier delivery confirmation").wait_for()
    page.get_by_role("button", name="Contest the dispute").click()
    page.get_by_text("Waiting for the cardmember's reply").first.wait_for()
    page.screenshot(path=os.path.join(SHOTS, "e2e-02-contested.png"), full_page=True)
    sign_out(page)


def test_e2e_03_rebuttal_done_then_the_worker_decides(page):
    sign_in(page, "cm-asha")
    page.get_by_role("row", name="C08").first.click()
    page.get_by_role("button", name="I have nothing more to add").click()
    # the worker decides within seconds; the screen refreshes itself while the case is being decided
    page.get_by_text("Decision: Charge stands (merchant upheld)").wait_for(timeout=20000)
    page.get_by_text("R4_MERCHANT_EVIDENCE_STRONGER").wait_for()
    page.screenshot(path=os.path.join(SHOTS, "e2e-03-decided.png"), full_page=True)
    sign_out(page)


def test_e2e_04_duplicate_charge_fast_path(page):
    sign_in(page, "cm-asha")
    page.get_by_role("row", name="ORD-5520").first.get_by_role("button", name="Dispute").click()
    page.get_by_label("I was charged twice for the same thing").check()
    page.get_by_label("What happened?").fill("I was charged twice for order ORD-5520.")
    page.get_by_role("button", name="File dispute").click()
    page.get_by_text("Decision: Refund to the cardmember").wait_for()
    page.get_by_text("FP1_DUPLICATE_CONFIRMED").wait_for()
    sign_out(page)


def test_e2e_05_auditor_verifies_and_downloads_a_checkpoint(page):
    sign_in(page, "aud-kabir")
    page.get_by_role("button", name="Verify chain").click()
    page.get_by_text("✔").wait_for()
    with page.expect_download() as download:
        page.get_by_role("button", name="Create checkpoint").click()
    checkpoint = json.load(open(download.value.path()))
    assert set(checkpoint) == {"seq", "entry_hash", "created_at", "key_id", "signature"}
    page.screenshot(path=os.path.join(SHOTS, "e2e-05-audit.png"), full_page=True)
    sign_out(page)


def test_e2e_06_reviewer_screens_and_no_script_errors(page):
    sign_in(page, "rev-neha")
    page.get_by_role("heading", name="Review queue").wait_for()
    page.get_by_role("heading", name="Metrics (cases closed in the period)").wait_for()
    page.screenshot(path=os.path.join(SHOTS, "e2e-06-reviewer.png"), full_page=True)
    sign_out(page)
    assert ERRORS == [], ERRORS
