"""Read-only queries for API views (no locks)."""
from sqlalchemy import text

from app.services.common import NotFound


async def load_dispute(session, dispute_id, principal):
    # the ownership predicate makes "not yours" identical to "does not exist"
    sql = "SELECT d.*, m.name AS merchant_name, t.order_ref, a.card_display_mask FROM disputes d " \
          "JOIN merchants m ON m.merchant_id = d.merchant_id " \
          "JOIN core_transactions t ON t.transaction_id = d.transaction_id " \
          "JOIN core_accounts a ON a.account_token = d.account_token WHERE d.dispute_id = :id"
    params = {"id": dispute_id}
    if principal["role"] == "cardmember":
        sql = sql + " AND d.cardmember_ref = :sub"
        params["sub"] = principal["sub"]
    if principal["role"] == "merchant":
        sql = sql + " AND d.merchant_id = :mid"
        params["mid"] = principal["merchant_id"]
    row = (await session.execute(text(sql), params)).mappings().one_or_none()
    if row is None:
        raise NotFound("dispute not found")
    dispute = dict(row)
    evidence = (await session.execute(text(
        "SELECT * FROM evidence_items WHERE dispute_id = :d ORDER BY seq"), {"d": dispute_id})).mappings().all()
    decision = None
    if dispute["current_decision_id"] is not None:
        decision = (await session.execute(text("SELECT * FROM decisions WHERE decision_id = :x"),
                                          {"x": dispute["current_decision_id"]})).mappings().one()
    offer = (await session.execute(text("SELECT * FROM settlement_offers WHERE dispute_id = :d"),
                                   {"d": dispute_id})).mappings().one_or_none()
    decision_dict = dict(decision) if decision else None
    offer_dict = dict(offer) if offer else None
    return dispute, [dict(e) for e in evidence], decision_dict, offer_dict


def allowed_actions(role, dispute, decision, offer, now):
    """Buttons the UI may show. The server re-checks every action anyway."""
    state = dispute["state"]
    actions = []
    can_appeal = (state == "DECIDED" and decision is not None and decision["appealable"]
                  and not dispute["appeal_used"] and now < decision["appeal_due_at"])
    if role == "cardmember":
        if state in ("AWAITING_MERCHANT", "AWAITING_CARDMEMBER_REBUTTAL"):
            actions = actions + ["add_evidence", "withdraw"]
        if state == "AWAITING_CARDMEMBER_REBUTTAL":
            actions.append("rebuttal_done")
        if state == "SETTLEMENT_OFFERED" and offer is not None and offer["cardmember_response"] is None:
            actions.append("respond_offer")
        if can_appeal:
            actions.append("appeal")
    if role == "merchant":
        if state == "AWAITING_MERCHANT":
            actions = actions + ["add_evidence", "contest", "accept"]
        if state == "SETTLEMENT_OFFERED" and offer is not None and offer["merchant_response"] is None:
            actions.append("respond_offer")
        if can_appeal:
            actions.append("appeal")
    if role == "reviewer" and state == "HUMAN_REVIEW":
        actions.append("review_decide")
    return actions
