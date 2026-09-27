"""Party and reviewer actions (FR-MERCH, FR-CM, FR-OFFER, FR-APPEAL, FR-REV). One transaction each."""
import hashlib
from datetime import timedelta
from sqlalchemy import text

from app.domain.redact import redact
from app.services.common import (Conflict, Invalid, lock_dispute, move, schedule_job, notify, notify_parties,
                            notify_reviewers, record_decision, append_event, actor_of)
from app.services.disputes import explanation_values
from app.domain.formatting import format_money


async def contest(session, ctx, principal, dispute_id, now):
    dispute = await lock_dispute(session, dispute_id, principal)
    merchant_items = (await session.execute(text(
        "SELECT count(*) AS n FROM evidence_items WHERE dispute_id = :d AND side = 'MERCHANT'"),
        {"d": dispute_id})).one().n
    if dispute["state"] == "AWAITING_MERCHANT" and merchant_items == 0:
        raise Invalid("add at least one piece of evidence before contesting", "evidence-required")
    due_at = now + timedelta(days=ctx.policy["windows"]["cardmember_rebuttal_days"])
    await move(session, ctx, dispute, "MERCHANT_CONTESTED", actor_of(principal), now, due_at=due_at)
    await schedule_job(session, dispute_id, "REBUTTAL_DEADLINE", due_at)
    await notify(session, "CARDMEMBER", dispute["cardmember_ref"], dispute_id,
                 "The merchant contested your dispute. Review their evidence and reply within "
                 f"{ctx.policy['windows']['cardmember_rebuttal_days']} days.")
    return dispute


async def accept(session, ctx, principal, dispute_id, now):
    dispute = await lock_dispute(session, dispute_id, principal)
    await move(session, ctx, dispute, "MERCHANT_ACCEPTED", actor_of(principal), now)
    await record_decision(session, ctx, dispute, "CARDMEMBER_REFUND", "MA_MERCHANT_ACCEPTED",
                          dispute["disputed_amount_minor"], "SYSTEM", now, explanation_values(ctx, dispute))
    return dispute


async def withdraw(session, ctx, principal, dispute_id, now):
    dispute = await lock_dispute(session, dispute_id, principal)
    await move(session, ctx, dispute, "CARDMEMBER_WITHDREW", actor_of(principal), now)
    await record_decision(session, ctx, dispute, "WITHDRAWN", "CW_CARDMEMBER_WITHDREW", 0, "SYSTEM", now,
                          explanation_values(ctx, dispute))
    return dispute


async def rebuttal_done(session, ctx, principal, dispute_id, now):
    dispute = await lock_dispute(session, dispute_id, principal)
    await move(session, ctx, dispute, "CARDMEMBER_REBUTTAL_DONE", actor_of(principal), now)
    await schedule_job(session, dispute_id, "DECIDE", now)
    return dispute


async def offer_response(session, ctx, principal, dispute_id, response, now):
    if response not in ("ACCEPTED", "DECLINED"):
        raise Invalid("response must be ACCEPTED or DECLINED")
    dispute = await lock_dispute(session, dispute_id, principal)
    if dispute["state"] != "SETTLEMENT_OFFERED":
        raise Conflict(f"no open offer in state {dispute['state']}")
    offer = (await session.execute(text(
        "SELECT * FROM settlement_offers WHERE dispute_id = :d FOR UPDATE"), {"d": dispute_id})).mappings().one()
    column = "cardmember_response" if principal["role"] == "cardmember" else "merchant_response"
    if offer[column] is not None:
        raise Conflict("you have already answered this offer", "offer-already-answered")
    # two fixed statements instead of building SQL from a variable column name
    if column == "cardmember_response":
        await session.execute(text("UPDATE settlement_offers SET cardmember_response = :r WHERE dispute_id = :d"),
                              {"r": response, "d": dispute_id})
    else:
        await session.execute(text("UPDATE settlement_offers SET merchant_response = :r WHERE dispute_id = :d"),
                              {"r": response, "d": dispute_id})
    await append_event(session, ctx, dispute_id, "OFFER_RESPONDED", actor_of(principal),
                       {"party": principal["role"].upper(), "response": response}, now)
    other_column = "merchant_response" if column == "cardmember_response" else "cardmember_response"
    if response == "DECLINED":
        await move(session, ctx, dispute, "OFFER_DECLINED", actor_of(principal), now, status_reason="OFFER_DECLINED")
        await notify_parties(session, dispute, "The settlement offer was declined. A reviewer will decide.")
        await notify_reviewers(session, dispute_id, "Offer declined: dispute needs review.")
    elif offer[other_column] == "ACCEPTED":
        await move(session, ctx, dispute, "OFFER_ACCEPTED_BY_BOTH", actor_of(principal), now)
        refund = offer["refund_amount_minor"]
        await record_decision(session, ctx, dispute, "SPLIT_SETTLEMENT", "OA_OFFER_ACCEPTED", refund, "SYSTEM", now,
                              explanation_values(ctx, dispute, {"refund": format_money(refund, dispute["currency"])}))
    return dispute


async def appeal(session, ctx, principal, dispute_id, reason, now):
    if not reason or len(reason) < 20 or len(reason) > 1000:
        raise Invalid("appeal reason must be 20-1000 characters")
    dispute = await lock_dispute(session, dispute_id, principal)
    if dispute["state"] != "DECIDED" or dispute["appeal_used"]:
        raise Conflict("this decision cannot be appealed", "appeal-not-allowed")
    decision = (await session.execute(text("SELECT appealable, appeal_due_at FROM decisions WHERE decision_id = :x"),
                                      {"x": dispute["current_decision_id"]})).one()
    if not decision.appealable or now >= decision.appeal_due_at:
        raise Conflict("this decision cannot be appealed", "appeal-not-allowed")
    by = "APPEAL_BY_CARDMEMBER" if principal["role"] == "cardmember" else "APPEAL_BY_MERCHANT"
    # the reason is redacted and kept for the reviewer; the ledger gets only its digest
    reason_redacted = redact(reason)
    await session.execute(text("UPDATE disputes SET appeal_used = true, appeal_reason_redacted = :r "
                               "WHERE dispute_id = :d"), {"r": reason_redacted, "d": dispute_id})
    await append_event(session, ctx, dispute_id, "APPEAL_FILED", actor_of(principal),
                       {"by": by, "reason_sha256": hashlib.sha256(reason_redacted.encode("utf-8")).hexdigest()}, now)
    await move(session, ctx, dispute, "APPEAL_FILED", actor_of(principal), now, status_reason=by)
    await notify_parties(session, dispute, "An appeal was filed. A reviewer will decide; no money moves until then.")
    await notify_reviewers(session, dispute_id, "Appeal filed: dispute needs review.")
    return dispute


async def review_decision(session, ctx, principal, dispute_id, verdict, refund_amount_minor, rationale, now):
    if verdict not in ("CARDMEMBER_REFUND", "MERCHANT_UPHELD", "SPLIT_SETTLEMENT"):
        raise Invalid("verdict must be CARDMEMBER_REFUND, MERCHANT_UPHELD or SPLIT_SETTLEMENT")
    if not rationale or len(rationale) < 20 or len(rationale) > 2000:
        raise Invalid("rationale must be 20-2000 characters")
    dispute = await lock_dispute(session, dispute_id, None)   # reviewers see every dispute
    amount = dispute["disputed_amount_minor"]
    if verdict == "SPLIT_SETTLEMENT":
        if refund_amount_minor is None or refund_amount_minor < 1 or refund_amount_minor > amount - 1:
            raise Invalid("split amount must be between 1 and the disputed amount minus 1")
        refund = refund_amount_minor
    elif verdict == "CARDMEMBER_REFUND":
        refund = amount
    else:
        refund = 0
    await move(session, ctx, dispute, "REVIEWER_DECIDED", actor_of(principal), now)
    await record_decision(session, ctx, dispute, verdict, "HR_REVIEWER_DECISION", refund, principal["sub"], now,
                          explanation_values(ctx, dispute, {"reviewer_rationale": redact(rationale)}))
    return dispute
