# The operational scripts: migrate (idempotent, detects edited migrations), seed, demo attacks detected by verify.
import asyncio, json, os, subprocess, sys, tempfile
from datetime import datetime, timezone
import pgserver
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from tests.paths import BACKEND, POLICY_PATH, ROLES_SQL, SEED_JSON, pg_host
from app.domain.policy import load_policy
from app.services.common import Ctx
from app.gateways.core import MockCore
from app.gateways.carrier import MockCarrier
from app.services import disputes, worker_jobs

PY = sys.executable


def run(args, env):
    return subprocess.run([PY] + args, cwd=str(BACKEND), env=dict(os.environ, **env), capture_output=True, text=True)  # noqa: S603 (fixed argv)


def test_scripts():
    srv = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    srv.psql(open(ROLES_SQL).read())
    sock = pg_host(srv)
    owner = f"postgresql://apex_owner@/apex?host={sock}"
    first = run(["scripts/migrate.py", "migrations"], {"MIGRATION_DATABASE_URL": owner})
    again = run(["scripts/migrate.py", "migrations"], {"MIGRATION_DATABASE_URL": owner})
    assert "applied 0001_initial.sql" in first.stdout and again.stdout == "", (first, again)
    refused_seed = run(["scripts/seed_demo.py", SEED_JSON], {"MIGRATION_DATABASE_URL": owner, "APP_ENV": "prod"})
    assert refused_seed.returncode != 0                      # T-INT-OPS-01: the demo seed never runs in prod
    seeded = run(["scripts/seed_demo.py", SEED_JSON], {"MIGRATION_DATABASE_URL": owner, "APP_ENV": "demo"})
    assert "demo data loaded" in seeded.stdout, seeded.stderr

    key = Ed25519PrivateKey.generate()
    ctx = Ctx(policy=load_policy(POLICY_PATH), signing_key=key, key_id="k-demo", core=MockCore(),
              carrier=MockCarrier(json.load(open(SEED_JSON))["carrier_fixtures"]))

    async def activity():
        engine = create_async_engine(f"postgresql+asyncpg://apex_app@/apex?host={sock}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        from app.bootstrap import register_public_key_and_load_all
        await register_public_key_and_load_all(sessions, ctx)
        now = datetime.now(timezone.utc)
        async with sessions() as s, s.begin():
            await disputes.file_dispute(s, ctx, {"sub": "cm-asha", "role": "cardmember"},
                                        {"transaction_id": "TXN-P08-B", "reason_code": "P08",
                                         "disputed_amount_minor": 129900, "statement": "charged twice"}, "demo-key-1", now)
        async with sessions() as s, s.begin():
            await disputes.file_dispute(s, ctx, {"sub": "cm-asha", "role": "cardmember"},
                                        {"transaction_id": "TXN-C08-OK", "reason_code": "C08",
                                         "disputed_amount_minor": 499900, "statement": "never came"}, "demo-key-2", now)
        async with sessions() as s, s.begin():
            cp = await worker_jobs.create_checkpoint(s, ctx, now)
        # T-SEC-15: the same key id registered with a DIFFERENT key stops startup
        impostor = Ctx(policy=ctx.policy, signing_key=Ed25519PrivateKey.generate(), key_id=ctx.key_id,
                       core=ctx.core, carrier=ctx.carrier)
        try:
            await register_public_key_and_load_all(sessions, impostor)
            conflict = "accepted"
        except RuntimeError as error:
            conflict = str(error)
        await engine.dispose()
        return cp, conflict

    checkpoint, conflict = asyncio.run(activity())
    assert conflict == "LEDGER_KEY_ID is already registered with a different key"
    cp_path = os.path.join(tempfile.mkdtemp(), "cp.json")
    json.dump(checkpoint, open(cp_path, "w"))
    audit_env = {"AUDIT_DATABASE_URL": owner}
    assert run(["scripts/verify_ledger.py", cp_path], audit_env).stdout.startswith("OK")
    integrity = run(["scripts/check_integrity.py"], {"DATABASE_URL": f"postgresql+asyncpg://apex_app@/apex?host={sock}"})
    assert integrity.stdout.startswith("OK: all 5 integrity checks passed"), (integrity.stdout, integrity.stderr)
    refused = run(["scripts/demo_ledger_attack.py", "tamper"], {"MIGRATION_DATABASE_URL": owner, "APP_ENV": "prod"})
    assert refused.returncode != 0
    run(["scripts/demo_ledger_attack.py", "truncate", "2"], {"MIGRATION_DATABASE_URL": owner, "APP_ENV": "demo"})
    truncated = run(["scripts/verify_ledger.py", cp_path], audit_env).stdout
    assert "truncated or rewritten" in truncated, truncated
    run(["scripts/demo_ledger_attack.py", "tamper"], {"MIGRATION_DATABASE_URL": owner, "APP_ENV": "demo"})
    tampered = run(["scripts/verify_ledger.py"], audit_env).stdout
    assert tampered.startswith("FAILED: hash mismatch"), tampered
    # an edited, already-applied migration is refused
    edited_dir = tempfile.mkdtemp()
    original = open(os.path.join(BACKEND, "migrations", "0001_initial.sql")).read()
    open(os.path.join(edited_dir, "0001_initial.sql"), "w").write(original + "\n-- edited\n")
    edited = run(["scripts/migrate.py", edited_dir], {"MIGRATION_DATABASE_URL": owner})
    assert "was modified after being applied" in edited.stderr
    print(truncated.strip(), "|", tampered.strip())
