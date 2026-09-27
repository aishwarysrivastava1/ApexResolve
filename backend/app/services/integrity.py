"""Integrity and reconciliation checks (APX-06 §7). Read-only; every query must return zero rows."""
from sqlalchemy import text

CHECKS = {
    "double entry: every transfer has one DEBIT and one CREDIT of the same amount": """
        SELECT transfer_id FROM core_ledger_lines GROUP BY transfer_id
        HAVING count(*) <> 2 OR sum(CASE WHEN direction = 'DEBIT' THEN amount_minor ELSE -amount_minor END) <> 0""",
    "every closed dispute that refunds money was paid exactly that amount": """
        SELECT d.dispute_id FROM disputes d
        JOIN decisions x ON x.decision_id = d.current_decision_id
        LEFT JOIN core_transfers t ON t.idempotency_key = 'dispute:' || d.dispute_id || ':decision:' || x.decision_id
        WHERE d.state = 'CLOSED' AND x.refund_amount_minor > 0
          AND (t.transfer_id IS NULL OR t.amount_minor <> x.refund_amount_minor)""",
    "the current decision belongs to its dispute": """
        SELECT d.dispute_id FROM disputes d JOIN decisions x ON x.decision_id = d.current_decision_id
        WHERE x.dispute_id <> d.dispute_id""",
    "the ledger head matches the last event": """
        SELECT h.last_seq FROM ledger_head h LEFT JOIN audit_events e ON e.seq = h.last_seq
        WHERE h.last_seq > 0 AND (e.seq IS NULL OR e.entry_hash <> h.last_hash)""",
    "every waiting state has a pending timer": """
        SELECT d.dispute_id FROM disputes d
        WHERE d.state IN ('AWAITING_MERCHANT', 'AWAITING_CARDMEMBER_REBUTTAL',
                          'READY_FOR_DECISION', 'SETTLEMENT_OFFERED')
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.dispute_id = d.dispute_id AND j.status = 'PENDING')""",
}


async def check(session):
    """Return a list of (check name, offending row count). An empty list means everything is consistent."""
    problems = []
    for name, sql in CHECKS.items():
        rows = (await session.execute(text(sql))).all()
        if rows:
            problems.append((name, len(rows)))
    return problems
