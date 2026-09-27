"""Filing a dispute (FR-FILE-01…10). One function, one transaction, explicit steps."""
import hashlib
import json
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.domain.eligibility import (filing_deadline, duplicate_check, credit_check, fast_path)
from app.domain.redact import redact
from app.services.common import (NotFound, Conflict, Invalid, RateLimited, append_event, schedule_job, notify,
                                 record_decision,
                            actor_of)
from app.domain.formatting import format_money, local_date_text

REASON_CODES = ["C08", "C31", "C02", "P08"]


def explanation_values(ctx, dispute, extra=None):
    """Every placeholder any template may need, computed once."""
    policy = ctx.policy
    zone = ZoneInfo(policy["timezone"])
    last_day = (dispute["filing_deadline_at"].astimezone(zone) - timedelta(days=1)).strftime("%d %b %Y")
    anchor_rule = policy["anchor_date"][dispute["reason_code"]]
    anchor_label = "the transaction date"
    if anchor_rule == "later_of_transaction_or_expected_delivery":
        anchor_label = "the later of the transaction date and the expected delivery date"
    values = {
        "deadline_local": last_day,
        "window_days": policy["windows"]["filing_window_days"],
        "anchor_label": anchor_label,
        "window_hours": policy["duplicate_check"]["window_hours"],
        "merchant_days": policy["windows"]["merchant_response_days"],
        "offer_days": policy["windows"]["settlement_offer_days"],
        "max_auto": format_money(policy["limits"]["max_auto_amount_minor"], policy["base_currency"]),
        "base_currency": policy["base_currency"],
    }
    if extra is not None:
        values.update(extra)
    return values


def request_hash(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


async def file_dispute(session, ctx, principal, body, idem_key, now):
    """body = {transaction_id, reason_code, disputed_amount_minor, statement} (already schema-validated).
    Returns (status_code, dispute_id, replayed)."""
    policy = ctx.policy
    limits = policy["limits"]
    sub = principal["sub"]
    body_hash = request_hash(body)

    # 1. idempotency: a replay returns the stored result
    stored = (await session.execute(text(
        "SELECT request_sha256, response_status, response_body FROM idempotency_keys "
        "WHERE subject = :s AND idem_key = :k"), {"s": sub, "k": idem_key})).one_or_none()
    if stored is not None:
        if stored.request_sha256 != body_hash:
            raise Invalid("Idempotency-Key was already used with a different request", "idempotency-key-reuse")
        return stored.response_status, uuid.UUID(stored.response_body["dispute_id"]), True

    # 2. the transaction must exist and belong to the caller (otherwise: not found)
    txn = await ctx.core.get_transaction(session, body["transaction_id"])
    if txn is None or txn["cardmember_ref"] != sub:
        raise NotFound("transaction not found")
    if txn["kind"] != "CHARGE":
        raise Invalid("only charges can be disputed", "not-a-charge")
    if body["reason_code"] not in REASON_CODES:
        raise Invalid("unsupported reason code")
    amount = body["disputed_amount_minor"]
    if amount < 1 or amount > txn["amount_minor"]:
        raise Invalid("disputed amount must be between 1 and the transaction amount", "amount-invalid")

    # 3. one dispute per transaction, ever
    existing = (await session.execute(text("SELECT 1 FROM disputes WHERE transaction_id = :t"),
                                      {"t": txn["transaction_id"]})).first()
    if existing is not None:
        raise Conflict("a dispute already exists for this transaction", "dispute-exists")

    # 4. quotas and the velocity flag (Math §3.4)
    window_start = now - timedelta(days=policy["risk"]["velocity_window_days"])
    counts = (await session.execute(text(
        "SELECT count(*) FILTER (WHERE state NOT IN ('CLOSED', 'REJECTED_INELIGIBLE')) AS open_count, "
        "count(*) FILTER (WHERE created_at >= :start AND created_at < :now) AS prior, "
        "count(*) FILTER (WHERE created_at >= :day_start) AS last_24h "
        "FROM disputes WHERE cardmember_ref = :s"),
        {"start": window_start, "now": now, "day_start": now - timedelta(days=1), "s": sub})).one()
    if counts.last_24h >= limits["max_filings_per_day"]:
        raise RateLimited("too many disputes filed in the last 24 hours")
    if counts.open_count >= limits["max_open_disputes_per_cardmember"]:
        raise Invalid("too many open disputes", "too-many-open-disputes")
    risk_flag = None
    if counts.prior >= policy["risk"]["velocity_prior_disputes"]:
        risk_flag = "HIGH_DISPUTE_VELOCITY"

    # 5. filing window (Math §3.2)
    deadline = filing_deadline(body["reason_code"], txn["transaction_at"], txn["expected_delivery_date"], policy)
    eligible = now < deadline

    # 6. closed-loop fact check and fast path (Math §4)
    fact_result = None
    fast = None
    if eligible and body["reason_code"] == "P08":
        similar = await ctx.core.list_similar_charges(session, txn, policy["duplicate_check"]["window_hours"])
        ids = [txn["transaction_id"]] + [c["transaction_id"] for c in similar]
        credits = await ctx.core.list_credits(session, ids)
        fact_result = duplicate_check(txn, [txn] + similar, credits, policy)
    if eligible and body["reason_code"] == "C02":
        credits = await ctx.core.list_credits(session, [txn["transaction_id"]])
        fact_result = credit_check(txn, credits, amount)
    if eligible and fact_result is not None:
        fast = fast_path(body["reason_code"], fact_result, amount, txn["currency"], policy)

    # 7. initial state
    if not eligible:
        state, status_reason, due_at = "REJECTED_INELIGIBLE", "E1_FILING_WINDOW_EXPIRED", None
    elif fast is not None:
        state, status_reason, due_at = "DECIDED", None, None
    else:
        state, status_reason = "AWAITING_MERCHANT", None
        due_at = now + timedelta(days=policy["windows"]["merchant_response_days"])

    # 8. insert the dispute (facts copied from core, never from the client)
    dispute_id = uuid.uuid4()
    dispute = {
        "dispute_id": dispute_id, "transaction_id": txn["transaction_id"], "cardmember_ref": sub,
        "account_token": txn["account_token"], "merchant_id": txn["merchant_id"], "se_number": txn["se_number"],
        "reason_code": body["reason_code"], "transaction_amount_minor": txn["amount_minor"],
        "disputed_amount_minor": amount, "currency": txn["currency"], "transaction_at": txn["transaction_at"],
        "expected_delivery_date": txn["expected_delivery_date"], "claim_received_at": now,
        "filing_deadline_at": deadline, "fact_check_result": fact_result, "risk_flag": risk_flag,
        "state": state, "state_entered_at": now, "state_due_at": due_at, "status_reason": status_reason,
        "policy_version": policy["policy_version"],
    }
    try:
        async with session.begin_nested():
            await session.execute(text(
                "INSERT INTO disputes (dispute_id, transaction_id, cardmember_ref, account_token, merchant_id, "
                "se_number, reason_code, transaction_amount_minor, disputed_amount_minor, currency, transaction_at, "
                "expected_delivery_date, claim_received_at, filing_deadline_at, fact_check_result, risk_flag, state, "
                "state_entered_at, state_due_at, status_reason, policy_version, created_at, updated_at) "
                "VALUES (:dispute_id, :transaction_id, "
                ":cardmember_ref, :account_token, :merchant_id, :se_number, :reason_code, :transaction_amount_minor, "
                ":disputed_amount_minor, :currency, :transaction_at, :expected_delivery_date, :claim_received_at, "
                ":filing_deadline_at, :fact_check_result, :risk_flag, :state, :state_entered_at, :state_due_at, "
                ":status_reason, :policy_version, :claim_received_at, :claim_received_at)"), dispute)
    except IntegrityError:
        # a concurrent filing for the same transaction won the race
        raise Conflict("a dispute already exists for this transaction", "dispute-exists") from None

    # 9. the statement is the first evidence item (redacted, self-attested)
    note = redact(body["statement"])
    await session.execute(text(
        "INSERT INTO evidence_items (evidence_id, dispute_id, seq, side, submitted_by, evidence_type, source, "
        "note_redacted, details, created_at) VALUES (:e, :d, 1, 'CARDMEMBER', 'CARDMEMBER', 'written_statement', "
        "'self_attested', :note, '{}'::jsonb, :now)"), {"e": uuid.uuid4(), "d": dispute_id, "note": note, "now": now})

    # 10. ledger: creation + evidence (no personal data in payloads)
    actor = actor_of(principal)
    await append_event(session, ctx, dispute_id, "DISPUTE_CREATED", actor, {
        "transaction_id": txn["transaction_id"], "reason_code": body["reason_code"],
        "disputed_amount_minor": amount, "currency": txn["currency"], "state": state,
        "status_reason": status_reason, "fact_check_result": fact_result, "risk_flag": risk_flag,
        "policy_version": policy["policy_version"]}, now)
    await append_event(session, ctx, dispute_id, "EVIDENCE_ADDED", actor, {
        "seq": 1, "side": "CARDMEMBER", "evidence_type": "written_statement", "source": "self_attested",
        "note_sha256": hashlib.sha256(note.encode("utf-8")).hexdigest()}, now)

    # 11. what happens next
    if fast is not None:
        verdict, rule_id = fast
        refund = amount if verdict == "CARDMEMBER_REFUND" else 0
        await record_decision(session, ctx, dispute, verdict, rule_id, refund, "SYSTEM", now,
                              explanation_values(ctx, dispute))
    elif eligible:
        await schedule_job(session, dispute_id, "MERCHANT_DEADLINE", due_at)
        await notify(session, "MERCHANT", txn["merchant_id"], dispute_id,
                     f"New dispute ({body['reason_code']}) for {format_money(amount, txn['currency'])}. "
                     f"Respond by {local_date_text(due_at, policy['timezone'])}.")

    # 12. remember the response for replays (same transaction)
    await session.execute(text(
        "INSERT INTO idempotency_keys (subject, idem_key, request_sha256, response_status, response_body) "
        "VALUES (:s, :k, :h, 201, CAST(:b AS jsonb))"),
        {"s": sub, "k": idem_key, "h": body_hash, "b": json.dumps({"dispute_id": str(dispute_id)})})
    return 201, dispute_id, False
