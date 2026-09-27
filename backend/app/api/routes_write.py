"""HTTP routes. Each handler: validate input -> call ONE service function in ONE transaction -> return a view."""
import json
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app.api.schemas import FileDisputeIn, OfferResponseIn, AppealIn, ReviewDecisionIn, dispute_view
from app.domain.explain import TEMPLATES, explain
from app.services.disputes import explanation_values
from app.api.security import (cardmember_only, merchant_only, party_only, reviewer_only, auditor_only, any_role)
from app.services import actions as actions_svc
from app.services import disputes as disputes_svc
from app.services import evidence as evidence_svc
from app.services import queries as queries_svc
from app.services import worker_jobs as worker_svc
from app.services.common import BadRequest, Invalid, NotFound

router = APIRouter(prefix="/api/v1")


def utcnow():
    return datetime.now(timezone.utc)


async def render(request, principal, dispute_id, status_code=200):
    # re-read after the command committed, then shape the view for this role
    async with request.app.state.sessions() as session:
        dispute, evidence, decision, offer = await queries_svc.load_dispute(session, dispute_id, principal)
    actions = queries_svc.allowed_actions(principal["role"], dispute, decision, offer, utcnow())
    # a status reason (E1, R1, R3, R7, offer declined/expired, appeals) is explained with its fixed template
    status_explanation = None
    if dispute["status_reason"] in TEMPLATES:
        ctx = request.app.state.ctx
        status_explanation = explain(dispute["status_reason"], explanation_values(ctx, dispute))
    view = dispute_view(dispute, evidence, decision, offer, principal["role"], actions, dispute["merchant_name"],
                        status_explanation)
    return JSONResponse(status_code=status_code, content=jsonable(view))


def jsonable(value):
    # datetimes -> RFC 3339 UTC text; everything else unchanged
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


async def run_command(request, fn, *args):
    # one command = one transaction
    async with request.app.state.sessions() as session:
        async with session.begin():
            return await fn(session, request.app.state.ctx, *args)


@router.post("/disputes", status_code=201)
async def file_dispute(request: Request, body: FileDisputeIn, principal=Depends(cardmember_only),
                       idempotency_key: Optional[str] = Header(default=None)):
    if idempotency_key is None:
        raise BadRequest("the Idempotency-Key header is required", "idempotency-key-required")
    if not 8 <= len(idempotency_key) <= 100:
        raise Invalid("Idempotency-Key must be 8-100 characters", "idempotency-key-invalid")
    status, dispute_id, _replayed = await run_command(request, disputes_svc.file_dispute, principal,
                                                      body.model_dump(), idempotency_key, utcnow())
    return await render(request, principal, dispute_id, status)


@router.get("/disputes/{dispute_id}")
async def get_dispute(request: Request, dispute_id: str, principal=Depends(any_role)):
    return await render(request, principal, parse_uuid(dispute_id))


@router.post("/disputes/{dispute_id}/evidence", status_code=201)
async def add_evidence(request: Request, dispute_id: str, principal=Depends(party_only),
                       kind: str = Form(...), evidence_type: Optional[str] = Form(None),
                       note: Optional[str] = Form(None), carrier: Optional[str] = Form(None),
                       tracking_number: Optional[str] = Form(None), file: Optional[UploadFile] = File(None)):
    data = None
    if file is not None:
        limit = request.app.state.ctx.policy["limits"]["max_file_bytes"]
        data = await file.read(limit + 1)     # read at most one byte past the limit
    form = {"kind": kind, "evidence_type": evidence_type, "note": note, "carrier": carrier,
            "tracking_number": tracking_number}
    await run_command(request, evidence_svc.add_evidence, principal, parse_uuid(dispute_id), form, data, utcnow())
    return await render(request, principal, parse_uuid(dispute_id), 201)


@router.post("/disputes/{dispute_id}/contest")
async def contest(request: Request, dispute_id: str, principal=Depends(merchant_only)):
    await run_command(request, actions_svc.contest, principal, parse_uuid(dispute_id), utcnow())
    return await render(request, principal, parse_uuid(dispute_id))


@router.post("/disputes/{dispute_id}/accept")
async def accept(request: Request, dispute_id: str, principal=Depends(merchant_only)):
    await run_command(request, actions_svc.accept, principal, parse_uuid(dispute_id), utcnow())
    return await render(request, principal, parse_uuid(dispute_id))


@router.post("/disputes/{dispute_id}/withdraw")
async def withdraw(request: Request, dispute_id: str, principal=Depends(cardmember_only)):
    await run_command(request, actions_svc.withdraw, principal, parse_uuid(dispute_id), utcnow())
    return await render(request, principal, parse_uuid(dispute_id))


@router.post("/disputes/{dispute_id}/rebuttal-done")
async def rebuttal_done(request: Request, dispute_id: str, principal=Depends(cardmember_only)):
    await run_command(request, actions_svc.rebuttal_done, principal, parse_uuid(dispute_id), utcnow())
    return await render(request, principal, parse_uuid(dispute_id))


@router.post("/disputes/{dispute_id}/offer-response")
async def offer_response(request: Request, dispute_id: str, body: OfferResponseIn, principal=Depends(party_only)):
    await run_command(request, actions_svc.offer_response, principal, parse_uuid(dispute_id), body.response,
                      utcnow())
    return await render(request, principal, parse_uuid(dispute_id))


@router.post("/disputes/{dispute_id}/appeal")
async def appeal(request: Request, dispute_id: str, body: AppealIn, principal=Depends(party_only)):
    await run_command(request, actions_svc.appeal, principal, parse_uuid(dispute_id), body.reason, utcnow())
    return await render(request, principal, parse_uuid(dispute_id))


@router.post("/disputes/{dispute_id}/review-decision")
async def review_decision(request: Request, dispute_id: str, body: ReviewDecisionIn, principal=Depends(reviewer_only)):
    await run_command(request, actions_svc.review_decision, principal, parse_uuid(dispute_id), body.verdict,
                      body.refund_amount_minor, body.rationale, utcnow())
    return await render(request, principal, parse_uuid(dispute_id))


@router.get("/disputes/{dispute_id}/files/{file_id}")
async def download_file(request: Request, dispute_id: str, file_id: str, principal=Depends(any_role)):
    if principal["role"] == "auditor":
        raise HTTPException(status_code=403, detail="auditors cannot download evidence files")
    async with request.app.state.sessions() as session:
        await queries_svc.load_dispute(session, parse_uuid(dispute_id), principal)   # ownership check
        row = (await session.execute(text(
            "SELECT content_type, data FROM evidence_files WHERE file_id = :f AND dispute_id = :d"),
            {"f": parse_uuid(file_id), "d": parse_uuid(dispute_id)})).one_or_none()
    if row is None:
        raise NotFound("file not found")
    extension = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}[row.content_type]
    return Response(content=row.data, media_type=row.content_type,
                    headers={"Content-Disposition": f'attachment; filename="evidence-{file_id}.{extension}"'})


@router.post("/audit/verify")
async def audit_verify(request: Request, principal=Depends(auditor_only)):
    body = await request.body()
    checkpoint = None
    if body:
        # a malformed checkpoint is the caller's mistake (422), never a server error
        try:
            checkpoint = json.loads(body)
        except ValueError:
            raise Invalid("the checkpoint must be the JSON returned by /audit/checkpoints") from None
        fields = ("entry_hash", "created_at", "key_id", "signature")
        if (not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("seq"), int)
                or not all(isinstance(checkpoint.get(f), str) for f in fields)):
            raise Invalid("the checkpoint must contain seq, entry_hash, created_at, key_id and signature")
    async with request.app.state.sessions() as session:
        ok, message = await worker_svc.verify_ledger(session, request.app.state.public_keys, checkpoint)
    return {"ok": ok, "message": message}


def parse_uuid(value):
    try:
        return uuid.UUID(value)
    except ValueError:
        raise NotFound("not found") from None
