"""Metrics summary (Math §14). Read-only SQL; returns plain dicts."""
from decimal import Decimal
from sqlalchemy import text


def ratio(numerator, denominator):
    if denominator == 0:
        return None
    return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001")))


async def summary(session, date_from, date_to):
    states = (await session.execute(text(
        "SELECT state, count(*) AS n FROM disputes GROUP BY state"))).mappings().all()
    closed = (await session.execute(text("""
        SELECT d.dispute_id, x.decided_by, x.rule_id,
               (SELECT count(*) FROM settlement_offers o WHERE o.dispute_id = d.dispute_id) AS had_offer,
               EXISTS (SELECT 1 FROM audit_events e WHERE e.dispute_id = d.dispute_id AND e.event_type = 'STATE_CHANGED'
                       AND (e.payload_canonical::jsonb ->> 'to') = 'HUMAN_REVIEW') AS had_review,
               EXISTS (SELECT 1 FROM audit_events e WHERE e.dispute_id = d.dispute_id AND e.event_type = 'APPEAL_FILED')
                       AS appealed,
               EXTRACT(EPOCH FROM (d.updated_at - d.claim_received_at)) / 86400.0 AS days_to_close
        FROM disputes d JOIN decisions x ON x.decision_id = d.current_decision_id
        WHERE d.state = 'CLOSED' AND d.updated_at::date BETWEEN :f AND :t"""),
        {"f": date_from, "t": date_to})).mappings().all()
    n = len(closed)
    automated = sum(1 for c in closed if c["decided_by"] == "SYSTEM")
    fast = sum(1 for c in closed if c["rule_id"].startswith("FP"))
    offers = sum(1 for c in closed if c["had_offer"])
    accepted = sum(1 for c in closed if c["rule_id"] == "OA_OFFER_ACCEPTED")
    reviewed = sum(1 for c in closed if c["had_review"])
    appealed = sum(1 for c in closed if c["appealed"])
    days = sorted(float(c["days_to_close"]) for c in closed)
    p50 = days[len(days) // 2] if days else None
    return {
        "counts_by_state": {r["state"]: r["n"] for r in states},
        "closed": n,
        "automation_rate": ratio(automated, n),
        "fast_path_rate": ratio(fast, n),
        "offer_rate": ratio(offers, n),
        "offer_acceptance_rate": ratio(accepted, offers),
        "human_review_rate": ratio(reviewed, n),
        "appeal_count": appealed,
        "time_to_close_days_p50": p50,
    }
