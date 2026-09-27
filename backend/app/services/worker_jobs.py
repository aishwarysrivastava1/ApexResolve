"""Worker: timers, decisions, finalization, outbox relay, checkpoints. Each unit of work = one transaction."""
import json
import uuid
from datetime import timedelta
from sqlalchemy import text

from app.domain.decision import decide, fraction_to_decimal_string
from app.domain.explain import explain
from app.domain import ledger_math as L
from app.services.common import (lock_dispute, move, schedule_job, notify_parties, notify_reviewers, record_decision,
                            append_event)
from app.services.disputes import explanation_values
from app.domain.formatting import format_money, pct_text, items_text

MAX_ATTEMPTS = 10


# ---------------------------------------------------------------- job handlers (dispute row locked inside)
async def handle_merchant_deadline(session, ctx, dispute_id, now):
    dispute = await lock_dispute(session, dispute_id)
    if dispute["state"] != "AWAITING_MERCHANT":
        return "stale"
    await move(session, ctx, dispute, "MERCHANT_DEADLINE_PASSED", "SYSTEM", now)
    await record_decision(session, ctx, dispute, "CARDMEMBER_REFUND", "R0_MERCHANT_NO_TIMELY_RESPONSE",
                          dispute["disputed_amount_minor"], "SYSTEM", now, explanation_values(ctx, dispute))
    return "done"


async def handle_rebuttal_deadline(session, ctx, dispute_id, now):
    dispute = await lock_dispute(session, dispute_id)
    if dispute["state"] != "AWAITING_CARDMEMBER_REBUTTAL":
        return "stale"
    await move(session, ctx, dispute, "REBUTTAL_DEADLINE_PASSED", "SYSTEM", now)
    await schedule_job(session, dispute_id, "DECIDE", now)
    return "done"


async def handle_decide(session, ctx, dispute_id, now):
    dispute = await lock_dispute(session, dispute_id)
    if dispute["state"] != "READY_FOR_DECISION":
        return "stale"
    rows = (await session.execute(text(
        "SELECT evidence_type, source FROM evidence_items WHERE dispute_id = :d ORDER BY seq"),
        {"d": dispute_id})).mappings().all()
    evidence = [dict(r) for r in rows]
    case = {"reason_code": dispute["reason_code"], "disputed_amount_minor": dispute["disputed_amount_minor"],
            "currency": dispute["currency"], "risk_flag": dispute["risk_flag"], "evidence": evidence}
    result = decide(case, ctx.policy)
    values = explanation_values(ctx, dispute, {
        "v_m_pct": pct_text(result["v_m"]), "v_cm_pct": pct_text(result["v_cm"]),
        "merchant_items": items_text(evidence, "MERCHANT", dispute["reason_code"], ctx.policy),
        "cardmember_items": items_text(evidence, "CARDMEMBER", dispute["reason_code"], ctx.policy),
    })
    if result["outcome"] == "DECIDED":
        await move(session, ctx, dispute, "AUTO_DECIDED", "SYSTEM", now)
        await record_decision(session, ctx, dispute, result["verdict"], result["rule_id"],
                              result["refund_amount_minor"], "SYSTEM", now, values, scores=result)
    elif result["outcome"] == "SETTLEMENT_OFFERED":
        refund = result["refund_amount_minor"]
        values["refund"] = format_money(refund, dispute["currency"])
        expires_at = now + timedelta(days=ctx.policy["windows"]["settlement_offer_days"])
        await session.execute(text(
            "INSERT INTO settlement_offers (dispute_id, refund_amount_minor, v_m, v_cm, margin, expires_at, "
            "explanation, "
            "created_at) VALUES (:d, :r, :vm, :vcm, :mg, :exp, :ex, :now)"),
            {"d": dispute_id, "r": refund, "vm": fraction_to_decimal_string(result["v_m"]),
             "vcm": fraction_to_decimal_string(result["v_cm"]), "mg": fraction_to_decimal_string(result["margin"]),
             "exp": expires_at, "ex": explain("R6_SETTLEMENT_OFFER", values), "now": now})
        await append_event(session, ctx, dispute_id, "OFFER_CREATED", "SYSTEM",
                           {"refund_amount_minor": refund, "margin": fraction_to_decimal_string(result["margin"]),
                            "policy_version": ctx.policy["policy_version"]}, now)
        await move(session, ctx, dispute, "OFFER_MADE", "SYSTEM", now, due_at=expires_at)
        await schedule_job(session, dispute_id, "OFFER_DEADLINE", expires_at)
        await notify_parties(session, dispute, f"Settlement offer: {values['refund']} to the cardmember. "
                                               "Both parties must accept.")
    else:
        await move(session, ctx, dispute, "SENT_TO_REVIEW", "SYSTEM", now, status_reason=result["rule_id"])
        await notify_reviewers(session, dispute_id, f"Needs review: {result['rule_id']}")
        await notify_parties(session, dispute, explain(result["rule_id"], values))
    return "done"


async def handle_offer_deadline(session, ctx, dispute_id, now):
    dispute = await lock_dispute(session, dispute_id)
    if dispute["state"] != "SETTLEMENT_OFFERED":
        return "stale"
    await move(session, ctx, dispute, "OFFER_DEADLINE_PASSED", "SYSTEM", now, status_reason="OFFER_EXPIRED")
    await notify_parties(session, dispute, "The settlement offer expired. A reviewer will decide.")
    await notify_reviewers(session, dispute_id, "Offer expired: dispute needs review.")
    return "done"


async def handle_finalize(session, ctx, dispute_id, now):
    dispute = await lock_dispute(session, dispute_id)
    if dispute["state"] != "DECIDED":
        return "stale"
    decision = (await session.execute(text("SELECT * FROM decisions WHERE decision_id = :x"),
                                      {"x": dispute["current_decision_id"]})).mappings().one()
    if decision["appealable"] and now < decision["appeal_due_at"]:
        return "not-yet"   # caller re-schedules at appeal_due_at
    if decision["refund_amount_minor"] > 0:
        key = f"dispute:{dispute_id}:decision:{decision['decision_id']}"
        payload = {"se_number": dispute["se_number"], "account_token": dispute["account_token"],
                   "amount_minor": decision["refund_amount_minor"], "currency": dispute["currency"]}
        await session.execute(text(
            "INSERT INTO outbox (outbox_id, dispute_id, kind, payload, idempotency_key, next_attempt_at) "
            "VALUES (:o, :d, 'DISPUTE_TRANSFER', CAST(:p AS jsonb), :k, :now)"),
            {"o": uuid.uuid4(), "d": dispute_id, "p": json.dumps(payload), "k": key, "now": now})
        await move(session, ctx, dispute, "FINALIZED_WITH_TRANSFER", "SYSTEM", now)
        await append_event(session, ctx, dispute_id, "TRANSFER_QUEUED", "SYSTEM",
                           {"idempotency_key": key, "amount_minor": decision["refund_amount_minor"],
                            "currency": dispute["currency"]}, now)
    else:
        await move(session, ctx, dispute, "FINALIZED_NO_TRANSFER", "SYSTEM", now)
    return "done"


HANDLERS = {
    "MERCHANT_DEADLINE": handle_merchant_deadline,
    "REBUTTAL_DEADLINE": handle_rebuttal_deadline,
    "DECIDE": handle_decide,
    "OFFER_DEADLINE": handle_offer_deadline,
    "FINALIZE": handle_finalize,
}


# ---------------------------------------------------------------- loop units
async def process_one_job(session_factory, ctx, now):
    """Claim and run ONE due job in its own transaction. Returns False when nothing is due."""
    job = None
    async with session_factory() as session:
        try:
            async with session.begin():
                job = (await session.execute(text(
                    "SELECT job_id, dispute_id, kind, attempts FROM jobs WHERE status = 'PENDING' AND run_at <= :now "
                    "ORDER BY run_at LIMIT 1 FOR UPDATE SKIP LOCKED"), {"now": now})).one_or_none()
                if job is None:
                    return False
                outcome = await HANDLERS[job.kind](session, ctx, job.dispute_id, now)
                if outcome == "not-yet":
                    due = (await session.execute(text(
                        "SELECT x.appeal_due_at FROM disputes d "
                        "JOIN decisions x ON x.decision_id = d.current_decision_id "
                        "WHERE d.dispute_id = :d"), {"d": job.dispute_id})).one().appeal_due_at
                    await session.execute(text("UPDATE jobs SET run_at = :r WHERE job_id = :j"),
                                          {"r": due, "j": job.job_id})
                else:
                    await session.execute(text(
                        "UPDATE jobs SET status = 'DONE', finished_at = :now WHERE job_id = :j"),
                        {"now": now, "j": job.job_id})
                return True
        except Exception as error:   # the whole unit rolled back; record the failure separately
            if job is None:
                raise
            async with session_factory() as s2, s2.begin():
                row = (await s2.execute(text("SELECT attempts FROM jobs WHERE job_id = :j"),
                                        {"j": job.job_id})).one()
                attempts = row.attempts + 1
                status = "FAILED" if attempts >= MAX_ATTEMPTS else "PENDING"
                await s2.execute(text(
                    "UPDATE jobs SET attempts = :a, status = :st, last_error = :e, run_at = :next WHERE job_id = :j"),
                    {"a": attempts, "st": status, "e": type(error).__name__,
                     "next": now + timedelta(seconds=min(2 ** attempts, 300)), "j": job.job_id})
                if status == "FAILED":
                    # a timer that can never run needs a person (APX-13 §9, runbook R2)
                    message = f"Timer {job.kind} failed permanently: needs operations."
                    await notify_reviewers(s2, job.dispute_id, message)
            return True


async def relay_one_outbox(session_factory, ctx, now):
    """Send ONE pending transfer. Returns False when nothing is pending."""
    row = None
    async with session_factory() as session:
        try:
            async with session.begin():
                row = (await session.execute(text(
                    "SELECT * FROM outbox WHERE status = 'PENDING' AND next_attempt_at <= :now "
                    "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"), {"now": now})).mappings().one_or_none()
                if row is None:
                    return False
                p = row["payload"]
                await ctx.core.transfer(session, row["idempotency_key"], row["dispute_id"], p["se_number"],
                                        p["account_token"], p["amount_minor"], p["currency"])
                await session.execute(text("UPDATE outbox SET status = 'SENT', sent_at = :now WHERE outbox_id = :o"),
                                      {"now": now, "o": row["outbox_id"]})
                dispute = await lock_dispute(session, row["dispute_id"])
                await move(session, ctx, dispute, "TRANSFER_CONFIRMED", "SYSTEM", now)
                await append_event(session, ctx, row["dispute_id"], "TRANSFER_CONFIRMED", "SYSTEM",
                                   {"idempotency_key": row["idempotency_key"], "amount_minor": p["amount_minor"]}, now)
                refund_text = format_money(p["amount_minor"], p["currency"])
                await notify_parties(session, dispute, f"Refund of {refund_text} sent.")
                return True
        except Exception as error:
            if row is None:
                raise
            async with session_factory() as s2, s2.begin():
                current = (await s2.execute(text("SELECT attempts, dispute_id FROM outbox WHERE outbox_id = :o"),
                                            {"o": row["outbox_id"]})).one()
                attempts = current.attempts + 1
                status = "DEAD" if attempts >= MAX_ATTEMPTS else "PENDING"
                await s2.execute(text(
                    "UPDATE outbox SET attempts = :a, status = :st, last_error = :e, next_attempt_at = :next "
                    "WHERE outbox_id = :o"),
                    {"a": attempts, "st": status, "e": type(error).__name__,
                     "next": now + timedelta(seconds=min(2 ** attempts, 300)), "o": row["outbox_id"]})
                if status == "DEAD":
                    await notify_reviewers(s2, current.dispute_id, "Transfer failed permanently: needs operations.")
            return True


# ---------------------------------------------------------------- checkpoints and verification
async def create_checkpoint(session, ctx, now):
    head = (await session.execute(text("SELECT last_seq, last_hash FROM ledger_head WHERE id = 1 FOR UPDATE"))).one()
    if head.last_seq == 0:
        return None
    created = L.format_timestamp(now)
    signature = L.sign_hash(ctx.signing_key, L.checkpoint_hash(head.last_seq, head.last_hash, created))
    await session.execute(text(
        "INSERT INTO ledger_checkpoints (seq, entry_hash, created_at_text, key_id, signature) "
        "VALUES (:s, :h, :c, :k, :sig)"),
        {"s": head.last_seq, "h": head.last_hash, "c": created, "k": ctx.key_id, "sig": signature})
    return {"seq": head.last_seq, "entry_hash": head.last_hash, "created_at": created, "key_id": ctx.key_id,
            "signature": signature}


async def maybe_auto_checkpoint(session_factory, ctx, now, every=100):
    async with session_factory() as session, session.begin():
        last_cp = (await session.execute(text("SELECT coalesce(max(seq), 0) AS s FROM ledger_checkpoints"))).one().s
        head = (await session.execute(text("SELECT last_seq FROM ledger_head WHERE id = 1"))).one().last_seq
        if head - last_cp >= every:
            return await create_checkpoint(session, ctx, now)
    return None


async def verify_ledger(session, public_keys, checkpoint=None):
    rows = (await session.execute(text("SELECT * FROM audit_events ORDER BY seq"))).mappings().all()
    events = []
    for r in rows:
        event = dict(r)
        event["dispute_id"] = str(event["dispute_id"])
        events.append(event)
    return L.verify_chain(events, public_keys, checkpoint)
