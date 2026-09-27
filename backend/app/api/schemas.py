"""Request models (strict) and response builders (role-scoped allow-lists)."""
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileDisputeIn(Strict):
    transaction_id: str = Field(min_length=1, max_length=64)
    reason_code: Literal["C08", "C31", "C02", "P08"]
    disputed_amount_minor: int = Field(ge=1)
    statement: str = Field(min_length=1, max_length=2000)


class OfferResponseIn(Strict):
    response: Literal["ACCEPTED", "DECLINED"]


class AppealIn(Strict):
    reason: str = Field(min_length=20, max_length=1000)


class ReviewDecisionIn(Strict):
    verdict: Literal["CARDMEMBER_REFUND", "MERCHANT_UPHELD", "SPLIT_SETTLEMENT"]
    refund_amount_minor: Optional[int] = None
    rationale: str = Field(min_length=20, max_length=2000)


def dispute_view(dispute, evidence, decision, offer, role, allowed_actions, merchant_name, status_explanation):
    """Build the DisputeView dict for one role (APX-07 §2.1)."""
    view = {
        "dispute_id": str(dispute["dispute_id"]), "reason_code": dispute["reason_code"], "state": dispute["state"],
        "state_due_at": dispute["state_due_at"], "status_reason": dispute["status_reason"],
        "status_explanation": status_explanation,
        "claim_received_at": dispute["claim_received_at"], "filing_deadline_at": dispute["filing_deadline_at"],
        "disputed_amount_minor": dispute["disputed_amount_minor"], "currency": dispute["currency"],
        "transaction": {"transaction_id": dispute["transaction_id"], "merchant_name": merchant_name,
                        "amount_minor": dispute["transaction_amount_minor"],
                        "transaction_at": dispute["transaction_at"], "order_ref": dispute["order_ref"]},
        "fact_check_result": dispute["fact_check_result"],
        "evidence": [], "decision": None, "offer": None,
        "allowed_actions": allowed_actions, "policy_version": dispute["policy_version"],
    }
    if role in ("reviewer", "auditor"):
        view["risk_flag"] = dispute["risk_flag"]
    if role in ("cardmember", "reviewer"):
        # the card mask is shown to the card's holder and to reviewers, never to merchants or auditors
        view["transaction"]["card_display_mask"] = dispute["card_display_mask"]
    if role != "auditor":
        # the (redacted) appeal reason is free text like evidence notes: parties and reviewers only
        view["appeal_reason"] = dispute["appeal_reason_redacted"]
    for item in evidence:
        entry = {"evidence_id": str(item["evidence_id"]), "seq": item["seq"], "side": item["side"],
                 "submitted_by": item["submitted_by"], "evidence_type": item["evidence_type"],
                 "source": item["source"], "details": item["details"], "created_at": item["created_at"],
                 "file_id": str(item["file_id"]) if item["file_id"] else None}
        if role != "auditor":
            entry["note"] = item["note_redacted"]
        view["evidence"].append(entry)
    if decision is not None:
        decided_by = decision["decided_by"]
        if role in ("cardmember", "merchant") and decided_by != "SYSTEM":
            decided_by = "REVIEWER"
        view["decision"] = {k: decision[k] for k in ("verdict", "rule_id", "refund_amount_minor", "v_m", "v_cm",
                                                     "margin", "appealable", "appeal_due_at", "explanation",
                                                     "created_at")}
        view["decision"]["decided_by"] = decided_by
    if offer is not None:
        view["offer"] = {k: offer[k] for k in ("refund_amount_minor", "expires_at", "cardmember_response",
                                                "merchant_response", "v_m", "v_cm", "margin", "explanation")}
    return view
