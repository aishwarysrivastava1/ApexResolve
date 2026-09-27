"""Adding evidence (FR-EVID-01…07)."""
import hashlib
import json
import re
import uuid
from sqlalchemy import text

from app.domain.eligibility import carrier_result_to_evidence
from app.domain.redact import redact
from app.services.common import Invalid, ServiceError, lock_dispute, append_event, actor_of
from app.services.files import prepare_upload, FileRejected
from app.gateways.carrier import SUPPORTED_CARRIERS

TRACKING_RE = re.compile(r"^[A-Za-z0-9]{8,30}$")
PARTY_STATES = {
    "MERCHANT": ["AWAITING_MERCHANT"],
    "CARDMEMBER": ["AWAITING_MERCHANT", "AWAITING_CARDMEMBER_REBUTTAL"],
}
TRACKING_RULES = {  # kind -> (who may submit, reason codes)
    "shipment_tracking": ("MERCHANT", ["C08"]),
    "return_tracking": ("CARDMEMBER", ["C31", "C02"]),
}


class UploadError(ServiceError):
    """File problems keep their own HTTP status (413 or 415)."""

    def __init__(self, status, problem, detail):
        super().__init__(detail, problem)
        self.status = status


async def item_from_file_or_text(session, policy, catalogue, party, dispute_id, form, file_bytes):
    """A document upload or a written statement. The source is decided here, never by the client."""
    kind = form.get("kind")
    evidence_type = form.get("evidence_type")
    entry = catalogue.get(evidence_type)
    # 1. the type must exist for this reason code, belong to this side and be produced this way
    if entry is None or entry["side"] != party or entry["produced_by"] != kind:
        raise Invalid("this evidence type is not allowed here", "evidence-not-allowed")
    item = {"evidence_type": evidence_type, "side": party, "submitted_by": party, "note": None,
            "details": {}, "file_id": None,
            "source": "document" if kind == "file" else "self_attested"}
    # 2. a statement needs text and no file
    if kind == "text":
        if not form.get("note") or len(form["note"]) > policy["limits"]["max_text_chars"]:
            raise Invalid("a statement of 1-2000 characters is required")
        if file_bytes:
            raise Invalid("text evidence cannot include a file", "evidence-not-allowed")
        item["note"] = redact(form["note"])
        return item
    # 3. a document needs a file; an optional note is redacted like any other free text
    if not file_bytes:
        raise Invalid("a file is required for this evidence type")
    if form.get("note"):
        if len(form["note"]) > policy["limits"]["max_text_chars"]:
            raise Invalid("note is too long")
        item["note"] = redact(form["note"])
    try:
        content_type, stored, sha = prepare_upload(file_bytes, policy["limits"]["max_file_bytes"])
    except FileRejected as rejected:
        raise UploadError(rejected.status, rejected.problem, rejected.detail) from None
    # 4. keep the cleaned bytes (images re-encoded) inside the database
    item["file_id"] = uuid.uuid4()
    await session.execute(text(
        "INSERT INTO evidence_files (file_id, dispute_id, uploaded_by, content_type, size_bytes, sha256, data) "
        "VALUES (:f, :d, :by, :ct, :sz, :sha, :data)"),
        {"f": item["file_id"], "d": dispute_id, "by": party, "ct": content_type, "sz": len(stored), "sha": sha,
         "data": stored})
    item["details"] = {"file_sha256": sha, "content_type": content_type}
    return item


async def item_from_tracking(session, ctx, catalogue, party, dispute, form):
    """A carrier tracking number, checked with the carrier. The carrier's answer decides type, side and source."""
    kind = form.get("kind")
    allowed_party, allowed_codes = TRACKING_RULES[kind]
    # 1. merchants prove delivery (C08); cardmembers prove a return (C31, C02)
    if party != allowed_party or dispute["reason_code"] not in allowed_codes:
        raise Invalid("tracking of this kind is not allowed here", "evidence-not-allowed")
    carrier = form.get("carrier")
    number = form.get("tracking_number") or ""
    if carrier not in SUPPORTED_CARRIERS:
        raise Invalid("unsupported carrier", "unsupported-carrier")
    if not TRACKING_RE.match(number):
        raise Invalid("tracking number must be 8-30 letters or digits")
    # 2. ask the carrier, and compare the delivery postcode with the one on Amex records
    result = await ctx.carrier.track(carrier, number)
    txn = await ctx.core.get_transaction(session, dispute["transaction_id"])
    expected_postcode = txn["shipping_postcode"] if kind == "shipment_tracking" else txn["return_postcode"]
    evidence_type, source = carrier_result_to_evidence(kind, result["status"], result["postcode"], expected_postcode)
    details = {"carrier": carrier, "tracking_number": number, "carrier_status": result["status"],
               "postcode_match": result["postcode"] is not None and result["postcode"] == expected_postcode,
               "triggered_by": party}
    return {"evidence_type": evidence_type, "side": catalogue[evidence_type]["side"], "source": source,
            "submitted_by": "SYSTEM" if source == "system_verified" else party, "note": None,
            "details": details, "file_id": None}


async def add_evidence(session, ctx, principal, dispute_id, form, file_bytes, now):
    """form = {kind, evidence_type?, note?, carrier?, tracking_number?}. Returns the list of created evidence ids."""
    policy = ctx.policy
    party = "CARDMEMBER" if principal["role"] == "cardmember" else "MERCHANT"
    dispute = await lock_dispute(session, dispute_id, principal)
    catalogue = policy["evidence_catalogue"][dispute["reason_code"]]
    kind = form.get("kind")

    # 1. the party may add evidence only in its allowed states
    if dispute["state"] not in PARTY_STATES[party]:
        raise Invalid(f"evidence cannot be added in state {dispute['state']}", "evidence-not-allowed")

    # 2. work out the item to create
    if kind in ("file", "text"):
        item = await item_from_file_or_text(session, policy, catalogue, party, dispute_id, form, file_bytes)
    elif kind in TRACKING_RULES:
        item = await item_from_tracking(session, ctx, catalogue, party, dispute, form)
    else:
        raise Invalid("unknown evidence kind", "evidence-not-allowed")
    side = item["side"]
    evidence_type = item["evidence_type"]
    source = item["source"]
    note = item["note"]
    details = item["details"]

    # 3. per-side limit
    count = (await session.execute(text(
        "SELECT count(*) AS n FROM evidence_items WHERE dispute_id = :d AND side = :s"),
        {"d": dispute_id, "s": side})).one().n
    if count >= policy["limits"]["max_evidence_items_per_side"]:
        raise Invalid("evidence limit reached for this side", "evidence-limit-reached")

    # 4. insert with the next sequence number (the dispute row is locked, so this is race-free)
    seq = (await session.execute(text(
        "SELECT coalesce(max(seq), 0) + 1 AS s FROM evidence_items WHERE dispute_id = :d"), {"d": dispute_id})).one().s
    evidence_id = uuid.uuid4()
    await session.execute(text(
        "INSERT INTO evidence_items (evidence_id, dispute_id, seq, side, submitted_by, evidence_type, source, "
        "note_redacted, details, file_id, created_at) VALUES (:e, :d, :seq, :side, :by, :t, :src, :note, "
        "CAST(:details AS jsonb), :f, :now)"),
        {"e": evidence_id, "d": dispute_id, "seq": seq, "side": side, "by": item["submitted_by"], "t": evidence_type,
         "src": source, "note": note, "details": json.dumps(details), "f": item["file_id"], "now": now})

    # 5. ledger (digests only, never the note or file)
    payload = {"seq": seq, "side": side, "evidence_type": evidence_type, "source": source,
               "submitted_by": item["submitted_by"]}
    if note is not None:
        payload["note_sha256"] = hashlib.sha256(note.encode("utf-8")).hexdigest()
    if "file_sha256" in details:
        payload["file_sha256"] = details["file_sha256"]
    if "carrier_status" in details:
        payload["carrier_status"] = details["carrier_status"]
    await append_event(session, ctx, dispute_id, "EVIDENCE_ADDED", actor_of(principal), payload, now)
    return [evidence_id]
