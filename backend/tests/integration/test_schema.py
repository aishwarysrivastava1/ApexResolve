# Validates roles, schema, constraints, grants and append-only triggers on real PostgreSQL 16.
import tempfile, uuid, asyncio
import asyncpg, pgserver

from tests.paths import ROLES_SQL, SCHEMA_SQL, pg_host

srv = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
print(srv.psql("select version();").splitlines()[2].strip())
print(srv.psql(open(ROLES_SQL).read()))
sock = pg_host(srv)


async def connect(user, db="apex"):
    return await asyncpg.connect(f"postgresql://{user}@/{db}?host={sock}")


async def expect_error(conn, sql, *args, contains=""):
    try:
        await conn.execute(sql, *args)
    except Exception as e:
        assert contains in str(e), (contains, str(e))
        return type(e).__name__ + ": " + str(e).splitlines()[0]
    raise AssertionError("expected an error for: " + sql)


async def main():
    owner = await connect("apex_owner")
    await owner.execute(open(SCHEMA_SQL).read())
    print("schema applied as apex_owner")
    # seed a little mock-core data as owner
    await owner.execute("""
        INSERT INTO merchants VALUES ('M_ACME', 'Acme Electronics', '400001');
        INSERT INTO merchant_se_numbers VALUES ('1000000001', 'M_ACME');
        INSERT INTO core_accounts VALUES ('acct_asha', 'cm-asha', 'OPEN', '110001', 'Asha', '371449*****8431');
        INSERT INTO core_transactions VALUES ('T-1001', 'acct_asha', '1000000001', 'CHARGE', 499900, 'INR',
            now() - interval '10 days', 'ORD-7781', current_date - 3, NULL);
        INSERT INTO ledger_keys VALUES ('k-dev-1', 'Ed25519', 'PEM', now());
    """)
    app = await connect("apex_app")
    did = uuid.uuid4()
    await app.execute("""
        INSERT INTO disputes (dispute_id, transaction_id, cardmember_ref, account_token, merchant_id, se_number,
            reason_code, transaction_amount_minor, disputed_amount_minor, currency, transaction_at,
            claim_received_at, filing_deadline_at, state, state_entered_at, policy_version)
        VALUES ($1, 'T-1001', 'cm-asha', 'acct_asha', 'M_ACME', '1000000001', 'C08', 499900, 499900, 'INR',
            now() - interval '10 days', now(), now() + interval '110 days', 'AWAITING_MERCHANT', now(), '2026.09.1')""", did)
    print("app role can file a dispute")
    # constraints
    print(await expect_error(app, """INSERT INTO disputes (dispute_id, transaction_id, cardmember_ref, account_token,
        merchant_id, se_number, reason_code, transaction_amount_minor, disputed_amount_minor, currency, transaction_at,
        claim_received_at, filing_deadline_at, state, state_entered_at, policy_version) VALUES ($1, 'T-1001', 'cm-asha',
        'acct_asha', 'M_ACME', '1000000001', 'C08', 499900, 499900, 'INR', now(), now(), now(), 'AWAITING_MERCHANT', now(), 'x')""",
        uuid.uuid4(), contains="duplicate key"))
    print(await expect_error(app, "UPDATE disputes SET state='EVALUATED' WHERE dispute_id=$1", did, contains="check constraint"))
    print(await expect_error(app, "UPDATE disputes SET disputed_amount_minor=999999 WHERE dispute_id=$1", did, contains="check constraint"))
    # ledger append + immutability
    await app.execute("""INSERT INTO audit_events VALUES (1, $1, 'DISPUTE_CREATED', 'cm-asha',
        '2026-09-26T10:00:00.000000Z', '{}', repeat('0',64), repeat('a',64), 'k-dev-1', 'sig')""", did)
    await app.execute("UPDATE ledger_head SET last_seq=1, last_hash=repeat('a',64) WHERE id=1")
    print(await expect_error(app, "UPDATE audit_events SET actor='x' WHERE seq=1", contains="permission denied"))
    print(await expect_error(owner, "UPDATE audit_events SET actor='x' WHERE seq=1", contains="append-only"))
    print(await expect_error(app, "DELETE FROM audit_events WHERE seq=1", contains="permission denied"))
    print(await expect_error(owner, "DELETE FROM audit_events WHERE seq=1", contains="append-only"))
    print(await expect_error(owner, "TRUNCATE audit_events CASCADE", contains="append-only"))
    print(await expect_error(app, """INSERT INTO audit_events VALUES (2, $1, 'STATE_CHANGED', 'x',
        '2026-09-26T10:00:00.000000Z', '{}', repeat('0',64), repeat('b',64), 'k-dev-1', 'sig')""", did, contains="duplicate key"))
    print(await expect_error(app, """INSERT INTO audit_events VALUES (3, $1, 'STATE_CHANGED', 'x',
        '2026-09-26 10:00:00', '{}', repeat('c',64), repeat('d',64), 'k-dev-1', 'sig')""", did, contains="check constraint"))
    # app cannot delete or run DDL, cannot write mock-core reference data
    print(await expect_error(app, "DELETE FROM disputes", contains="permission denied"))
    print(await expect_error(app, "DROP TABLE jobs", contains="must be owner"))
    print(await expect_error(app, "UPDATE core_transactions SET amount_minor=1", contains="permission denied"))
    # decisions check: MERCHANT_UPHELD must have refund 0
    print(await expect_error(app, """INSERT INTO decisions (decision_id, dispute_id, verdict, rule_id, refund_amount_minor,
        policy_version, decided_by, appealable, appeal_due_at, explanation) VALUES ($1, $2, 'MERCHANT_UPHELD', 'R4', 100,
        'v', 'SYSTEM', false, NULL, 'x')""", uuid.uuid4(), did, contains="check constraint"))
    # double-entry transfer via app role
    tid = uuid.uuid4()
    await app.execute("INSERT INTO core_transfers (transfer_id, idempotency_key, dispute_id, amount_minor, currency) VALUES ($1,'k1',$2,100,'INR')", tid, did)
    await app.execute("INSERT INTO core_ledger_lines (transfer_id, account_ref, direction, amount_minor) VALUES ($1,'SE:1000000001','DEBIT',100),($1,'CARD:acct_asha','CREDIT',100)", tid)
    print(await expect_error(app, "INSERT INTO core_transfers (idempotency_key, dispute_id, amount_minor, currency) VALUES ('k1',$1,100,'INR')", did, contains="duplicate key"))
    # T-INT-DB-02: every append-only table refuses UPDATE and DELETE, even for the owner
    fid = uuid.uuid4()
    await app.execute("""INSERT INTO evidence_files (file_id, dispute_id, uploaded_by, content_type, size_bytes, sha256, data)
        VALUES ($1, $2, 'MERCHANT', 'application/pdf', 3, repeat('e', 64), 'abc'::bytea)""", fid, did)
    await app.execute("""INSERT INTO evidence_items (evidence_id, dispute_id, seq, side, submitted_by, evidence_type, source)
        VALUES ($1, $2, 1, 'CARDMEMBER', 'CARDMEMBER', 'written_statement', 'self_attested')""", uuid.uuid4(), did)
    await app.execute("""INSERT INTO decisions (decision_id, dispute_id, verdict, rule_id, refund_amount_minor,
        policy_version, decided_by, appealable, appeal_due_at, explanation) VALUES ($1, $2, 'CARDMEMBER_REFUND',
        'MA_MERCHANT_ACCEPTED', 100, 'v', 'SYSTEM', false, NULL, 'x')""", uuid.uuid4(), did)
    await app.execute("""INSERT INTO ledger_checkpoints (seq, entry_hash, created_at_text, key_id, signature)
        VALUES (1, repeat('a', 64), '2026-09-26T10:00:00.000000Z', 'k-dev-1', 'sig')""")
    columns = {"evidence_files": "size_bytes", "evidence_items": "seq", "decisions": "rule_id",
               "ledger_checkpoints": "seq", "core_ledger_lines": "amount_minor"}
    for table, column in columns.items():
        print(await expect_error(owner, f"UPDATE {table} SET {column} = {column}", contains="append-only"))  # noqa: S608
        print(await expect_error(owner, f"DELETE FROM {table}", contains="append-only"))  # noqa: S608
    print("ALL SCHEMA CHECKS PASSED")


def test_main():
    asyncio.run(main())
