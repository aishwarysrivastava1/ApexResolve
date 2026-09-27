"""Read-only and auxiliary endpoints: lists, timeline, review queue, audit, metrics, notifications, policy."""
import base64
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import text

from app.api.routes_write import jsonable, parse_uuid, utcnow
from app.api.security import (cardmember_only, reviewer_only, auditor_only, staff_only, any_role)
from app.services import worker_jobs as worker_svc, metrics as metrics_svc, queries as queries_svc
from app.services.common import NotFound, Invalid

read_router = APIRouter(prefix="/api/v1")


def encode_cursor(moment, item_id):
    raw = f"{moment.astimezone(timezone.utc).isoformat()}|{item_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor):
    try:
        moment_text, item_id = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8").split("|")
        return datetime.fromisoformat(moment_text), item_id
    except Exception:
        raise Invalid("invalid cursor") from None


@read_router.get("/me/transactions")
async def my_transactions(request: Request, principal=Depends(cardmember_only), limit: int = Query(50, ge=1, le=100)):
    since = utcnow() - timedelta(days=180)
    async with request.app.state.sessions() as session:
        rows = (await session.execute(text(
            "SELECT t.transaction_id, m.name AS merchant_name, t.amount_minor, t.currency, t.transaction_at, "
            "t.order_ref, a.card_display_mask, d.dispute_id "
            "FROM core_transactions t JOIN core_accounts a ON a.account_token = t.account_token "
            "JOIN merchant_se_numbers s ON s.se_number = t.se_number JOIN merchants m ON m.merchant_id = s.merchant_id "
            "LEFT JOIN disputes d ON d.transaction_id = t.transaction_id "
            "WHERE a.cardmember_ref = :sub AND t.kind = 'CHARGE' AND t.transaction_at >= :since "
            "ORDER BY t.transaction_at DESC LIMIT :limit"),
            {"sub": principal["sub"], "since": since, "limit": limit})).mappings().all()
    items = [jsonable({**dict(r), "dispute_id": str(r["dispute_id"]) if r["dispute_id"] else None}) for r in rows]
    return {"items": items, "next_cursor": None}


@read_router.get("/disputes")
async def list_disputes(request: Request, principal=Depends(any_role), state: Optional[List[str]] = Query(None),
                        limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = None):
    sql = ("SELECT d.dispute_id, d.reason_code, d.state, d.state_due_at, d.status_reason, d.disputed_amount_minor, "
           "d.currency, d.transaction_id, d.created_at, m.name AS merchant_name "
           "FROM disputes d JOIN merchants m ON m.merchant_id = d.merchant_id WHERE true")
    params = {"limit": limit}
    if principal["role"] == "cardmember":
        sql = sql + " AND d.cardmember_ref = :sub"
        params["sub"] = principal["sub"]
    if principal["role"] == "merchant":
        sql = sql + " AND d.merchant_id = :mid"
        params["mid"] = principal["merchant_id"]
    if state:
        sql = sql + " AND d.state = ANY(:states)"
        params["states"] = state
    if cursor:
        moment, item_id = decode_cursor(cursor)
        sql = sql + " AND (d.created_at, d.dispute_id) < (:c_at, :c_id)"
        params["c_at"] = moment
        params["c_id"] = parse_uuid(item_id)
    sql = sql + " ORDER BY d.created_at DESC, d.dispute_id DESC LIMIT :limit"
    async with request.app.state.sessions() as session:
        rows = (await session.execute(text(sql), params)).mappings().all()
    items = [jsonable({**dict(r), "dispute_id": str(r["dispute_id"])}) for r in rows]
    next_cursor = None
    if len(rows) == limit:
        next_cursor = encode_cursor(rows[-1]["created_at"], rows[-1]["dispute_id"])
    return {"items": items, "next_cursor": next_cursor}


@read_router.get("/disputes/{dispute_id}/timeline")
async def timeline(request: Request, dispute_id: str, principal=Depends(any_role)):
    async with request.app.state.sessions() as session:
        await queries_svc.load_dispute(session, parse_uuid(dispute_id), principal)   # ownership check
        rows = (await session.execute(text(
            "SELECT seq, event_type, actor, created_at_text, payload_canonical::jsonb AS payload FROM audit_events "
            "WHERE dispute_id = :d ORDER BY seq"), {"d": parse_uuid(dispute_id)})).mappings().all()
    items = []
    for r in rows:
        item = {"seq": r["seq"], "event_type": r["event_type"], "at": r["created_at_text"],
                "actor_role": r["actor"].split(":")[0].upper()}
        if r["event_type"] == "STATE_CHANGED":
            item["summary"] = f"{r['payload']['from']} → {r['payload']['to']}"
        if principal["role"] in ("reviewer", "auditor"):
            item["actor"] = r["actor"]
        items.append(item)
    return {"items": items}


@read_router.get("/review/queue")
async def review_queue(request: Request, principal=Depends(reviewer_only), limit: int = Query(50, ge=1, le=100)):
    async with request.app.state.sessions() as session:
        rows = (await session.execute(text(
            "SELECT d.dispute_id, d.reason_code, d.disputed_amount_minor, d.currency, d.status_reason, "
            "d.state_entered_at AS waiting_since, o.v_m, o.v_cm, o.margin "
            "FROM disputes d LEFT JOIN settlement_offers o ON o.dispute_id = d.dispute_id "
            "WHERE d.state = 'HUMAN_REVIEW' ORDER BY d.state_entered_at LIMIT :limit"),
            {"limit": limit})).mappings().all()
    return {"items": [jsonable({**dict(r), "dispute_id": str(r["dispute_id"])}) for r in rows], "next_cursor": None}


@read_router.get("/audit/events")
async def audit_events(request: Request, principal=Depends(auditor_only), dispute_id: Optional[str] = None,
                       after_seq: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
    sql = "SELECT * FROM audit_events WHERE seq > :after"
    params = {"after": after_seq, "limit": limit}
    if dispute_id:
        sql = sql + " AND dispute_id = :d"
        params["d"] = parse_uuid(dispute_id)
    async with request.app.state.sessions() as session:
        rows = (await session.execute(text(sql + " ORDER BY seq LIMIT :limit"), params)).mappings().all()
    return {"items": [jsonable({**dict(r), "dispute_id": str(r["dispute_id"])}) for r in rows]}


@read_router.post("/audit/checkpoints")
async def create_checkpoint(request: Request, principal=Depends(auditor_only)):
    async with request.app.state.sessions() as session, session.begin():
        checkpoint = await worker_svc.create_checkpoint(session, request.app.state.ctx, utcnow())
    if checkpoint is None:
        raise NotFound("the ledger is empty")
    return checkpoint


@read_router.get("/audit/checkpoints/latest")
async def latest_checkpoint(request: Request, principal=Depends(auditor_only)):
    async with request.app.state.sessions() as session:
        row = (await session.execute(text(
            "SELECT seq, entry_hash, created_at_text AS created_at, key_id, signature FROM ledger_checkpoints "
            "ORDER BY checkpoint_id DESC LIMIT 1"))).mappings().one_or_none()
    if row is None:
        raise NotFound("no checkpoint yet")
    return dict(row)


@read_router.get("/audit/public-keys")
async def public_keys(request: Request, principal=Depends(auditor_only)):
    async with request.app.state.sessions() as session:
        rows = (await session.execute(text(
            "SELECT key_id, algorithm, public_key_pem, created_at FROM ledger_keys ORDER BY created_at"
        ))).mappings().all()
    return {"keys": [jsonable(dict(r)) for r in rows]}


@read_router.get("/metrics/summary")
async def metrics_summary(request: Request, principal=Depends(staff_only), date_from: str = Query(alias="from"),
                          date_to: str = Query(alias="to")):
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
        raise Invalid("from/to must be YYYY-MM-DD") from None
    async with request.app.state.sessions() as session:
        result = await metrics_svc.summary(session, start, end)
    return {"period": {"from": date_from, "to": date_to}, **result}


def recipient_of(principal):
    # whose inbox the caller reads: their own, their merchant's, or the shared reviewer inbox; auditors have none
    if principal["role"] == "cardmember":
        return "CARDMEMBER", principal["sub"]
    if principal["role"] == "merchant":
        return "MERCHANT", principal["merchant_id"]
    if principal["role"] == "reviewer":
        return "REVIEWER", "*"
    return None, None


@read_router.get("/notifications")
async def notifications(request: Request, principal=Depends(any_role), unread: bool = False):
    role, ref = recipient_of(principal)
    if role is None:
        return {"items": []}
    sql = ("SELECT notification_id, dispute_id, message, created_at, read_at FROM notifications "
           "WHERE recipient_role = :role AND recipient_ref = :ref")
    if unread:
        sql = sql + " AND read_at IS NULL"
    async with request.app.state.sessions() as session:
        rows = (await session.execute(text(sql + " ORDER BY created_at DESC LIMIT 100"),
                                      {"role": role, "ref": ref})).mappings().all()
    return {"items": [jsonable({**dict(r), "notification_id": str(r["notification_id"]),
                                "dispute_id": str(r["dispute_id"]) if r["dispute_id"] else None}) for r in rows]}


@read_router.post("/notifications/{notification_id}/read", status_code=204)
async def mark_read(request: Request, notification_id: str, principal=Depends(any_role)):
    role, ref = recipient_of(principal)
    if role is None:
        return Response(status_code=204)       # auditors have no inbox; nothing to mark
    async with request.app.state.sessions() as session, session.begin():
        await session.execute(text(
            "UPDATE notifications SET read_at = now() WHERE notification_id = :n AND recipient_role = :role "
            "AND recipient_ref = :ref AND read_at IS NULL"),
            {"n": parse_uuid(notification_id), "role": role, "ref": ref})
    return Response(status_code=204)


@read_router.get("/policy")
async def policy(request: Request, principal=Depends(any_role)):
    return request.app.state.ctx.policy
