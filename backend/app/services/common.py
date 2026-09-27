"""Shared service-layer building blocks: context, errors, locking, transitions, jobs, notifications, decisions.

Copy into backend/app/services/common.py. Every function here expects the caller to have
opened ONE transaction (`async with session.begin():`) and passes `now` explicitly (testable time).
"""
import uuid
from dataclasses import dataclass
from datetime import timedelta
from sqlalchemy import text

from app.domain import fsm
from app.domain import ledger_math as L
from app.domain.decision import fraction_to_decimal_string
from app.domain.explain import explain


# ---------------------------------------------------------------- context and errors
@dataclass
class Ctx:
    policy: dict            # validated policy dict (app/domain/policy.py)
    signing_key: object     # Ed25519PrivateKey
    key_id: str             # key id registered in ledger_keys
    core: object            # CoreGateway implementation
    carrier: object         # CarrierGateway implementation


class ServiceError(Exception):
    """Base class; the API maps subclasses to RFC 9457 problems."""
    status = 500
    problem = "internal-error"

    def __init__(self, detail="", problem=None):
        super().__init__(detail)
        self.detail = detail
        if problem is not None:
            self.problem = problem


class NotFound(ServiceError):
    status, problem = 404, "not-found"


class Conflict(ServiceError):
    status, problem = 409, "illegal-state"


class Invalid(ServiceError):
    status, problem = 422, "validation-error"


class RateLimited(ServiceError):
    status, problem = 429, "rate-limited"
    retry_after = 3600      # the daily filing quota is a rolling 24 h window; ask the client to wait an hour


class BadRequest(ServiceError):
    status, problem = 400, "bad-request"


# ---------------------------------------------------------------- ledger append
async def append_event(session, ctx, dispute_id, event_type, actor, payload, now):
    # lock the single head row; every append in the system waits here, so the chain never forks
    head = (await session.execute(text(
        "SELECT last_seq, last_hash FROM ledger_head WHERE id = 1 FOR UPDATE"))).one()
    seq = head.last_seq + 1
    created_at_text = L.format_timestamp(now)
    payload_canonical = L.canonical_json(payload)
    entry_hash = L.entry_hash(seq, head.last_hash, str(dispute_id), event_type, actor, created_at_text,
                              payload_canonical)
    signature = L.sign_hash(ctx.signing_key, entry_hash)
    await session.execute(text(
        "INSERT INTO audit_events (seq, dispute_id, event_type, actor, created_at_text, payload_canonical, "
        "prev_hash, entry_hash, key_id, signature) VALUES (:seq, :did, :et, :actor, :ts, :pl, :prev, :h, :kid, :sig)"),
        {"seq": seq, "did": dispute_id, "et": event_type, "actor": actor, "ts": created_at_text,
         "pl": payload_canonical, "prev": head.last_hash, "h": entry_hash, "kid": ctx.key_id, "sig": signature})
    await session.execute(text("UPDATE ledger_head SET last_seq = :seq, last_hash = :h WHERE id = 1"),
                          {"seq": seq, "h": entry_hash})
    return seq


# ---------------------------------------------------------------- dispute locking and access
async def lock_dispute(session, dispute_id, principal=None):
    """Lock the dispute row. For parties, the ownership check is part of the query (not yours = not found)."""
    sql = "SELECT * FROM disputes WHERE dispute_id = :id"
    params = {"id": dispute_id}
    if principal is not None and principal["role"] == "cardmember":
        sql = sql + " AND cardmember_ref = :sub"
        params["sub"] = principal["sub"]
    if principal is not None and principal["role"] == "merchant":
        sql = sql + " AND merchant_id = :mid"
        params["mid"] = principal["merchant_id"]
    row = (await session.execute(text(sql + " FOR UPDATE"), params)).mappings().one_or_none()
    if row is None:
        raise NotFound("dispute not found")
    return dict(row)


def actor_of(principal):
    # ledger actor: the subject for people, SYSTEM for the worker
    if principal is None:
        return "SYSTEM"
    return f"{principal['role']}:{principal['sub']}"


async def move(session, ctx, dispute, event, actor, now, due_at=None, status_reason=None):
    """Apply one FSM event to a LOCKED dispute row (dict). Raises Conflict for illegal events."""
    try:
        new_state = fsm.next_state(dispute["state"], event)
    except fsm.IllegalTransition as error:
        raise Conflict(str(error)) from None
    await session.execute(text(
        "UPDATE disputes SET state = :s, state_entered_at = :now, state_due_at = :due, status_reason = :reason, "
        "updated_at = :now WHERE dispute_id = :id"),
        {"s": new_state, "now": now, "due": due_at, "reason": status_reason, "id": dispute["dispute_id"]})
    await append_event(session, ctx, dispute["dispute_id"], "STATE_CHANGED", actor,
                       {"event": event, "from": dispute["state"], "to": new_state}, now)
    old_state = dispute["state"]
    dispute["state"] = new_state
    dispute["state_due_at"] = due_at
    dispute["status_reason"] = status_reason
    return old_state, new_state


async def schedule_job(session, dispute_id, kind, run_at):
    await session.execute(text("INSERT INTO jobs (job_id, dispute_id, kind, run_at) VALUES (:j, :d, :k, :r)"),
                          {"j": uuid.uuid4(), "d": dispute_id, "k": kind, "r": run_at})


async def notify(session, recipient_role, recipient_ref, dispute_id, message):
    await session.execute(text(
        "INSERT INTO notifications (notification_id, recipient_role, recipient_ref, dispute_id, message) "
        "VALUES (:n, :role, :ref, :d, :m)"),
        {"n": uuid.uuid4(), "role": recipient_role, "ref": recipient_ref, "d": dispute_id, "m": message[:500]})


async def notify_parties(session, dispute, message):
    await notify(session, "CARDMEMBER", dispute["cardmember_ref"], dispute["dispute_id"], message)
    await notify(session, "MERCHANT", dispute["merchant_id"], dispute["dispute_id"], message)


async def notify_reviewers(session, dispute_id, message):
    await notify(session, "REVIEWER", "*", dispute_id, message)


# ---------------------------------------------------------------- decisions
APPEALABLE_RULES = ["FP1_DUPLICATE_CONFIRMED", "FP2_NO_DUPLICATE_FOUND", "FP3_CREDIT_ALREADY_POSTED",
                    "R0_MERCHANT_NO_TIMELY_RESPONSE", "R4_MERCHANT_EVIDENCE_STRONGER",
                    "R5_CARDMEMBER_EVIDENCE_STRONGER"]


async def record_decision(session, ctx, dispute, verdict, rule_id, refund_amount_minor, decided_by, now,
                          explanation_values, scores=None):
    """Insert a decision, point the dispute at it, log it, and schedule FINALIZE."""
    appealable = rule_id in APPEALABLE_RULES
    appeal_due_at = now + timedelta(days=ctx.policy["windows"]["appeal_window_days"]) if appealable else None
    decision_id = uuid.uuid4()
    v_m = v_cm = margin = None
    if scores is not None:
        v_m = fraction_to_decimal_string(scores["v_m"])
        v_cm = fraction_to_decimal_string(scores["v_cm"])
        margin = fraction_to_decimal_string(scores["margin"])
    explanation = explain(rule_id, explanation_values)
    await session.execute(text(
        "INSERT INTO decisions (decision_id, dispute_id, verdict, rule_id, refund_amount_minor, v_m, v_cm, margin, "
        "policy_version, decided_by, appealable, appeal_due_at, explanation, created_at) VALUES "
        "(:id, :d, :v, :r, :amt, :vm, :vcm, :mg, :pv, :by, :ap, :due, :ex, :now)"),
        {"id": decision_id, "d": dispute["dispute_id"], "v": verdict, "r": rule_id, "amt": refund_amount_minor,
         "vm": v_m, "vcm": v_cm, "mg": margin, "pv": ctx.policy["policy_version"], "by": decided_by,
         "ap": appealable, "due": appeal_due_at, "ex": explanation, "now": now})
    await session.execute(text("UPDATE disputes SET current_decision_id = :x WHERE dispute_id = :d"),
                          {"x": decision_id, "d": dispute["dispute_id"]})
    dispute["current_decision_id"] = decision_id
    await append_event(session, ctx, dispute["dispute_id"], "DECISION_RECORDED",
                       "SYSTEM" if decided_by == "SYSTEM" else f"reviewer:{decided_by}",
                       {"decision_id": str(decision_id), "verdict": verdict, "rule_id": rule_id,
                        "refund_amount_minor": refund_amount_minor, "v_m": v_m, "v_cm": v_cm, "margin": margin,
                        "policy_version": ctx.policy["policy_version"], "appealable": appealable}, now)
    # final decisions settle now; appealable ones after the window closes
    await schedule_job(session, dispute["dispute_id"], "FINALIZE", appeal_due_at if appealable else now)
    await notify_parties(session, dispute, f"Decision: {verdict.replace('_', ' ').lower()}. {explanation}")
    return decision_id
