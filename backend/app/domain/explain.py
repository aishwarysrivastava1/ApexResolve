"""Deterministic explanation templates, one per rule id. Pure; no LLM."""

TEMPLATES = {
    "E1_FILING_WINDOW_EXPIRED": "This claim was received after the last filing day ({deadline_local}). "
                                "The filing window is {window_days} days from {anchor_label}.",
    "FP1_DUPLICATE_CONFIRMED": "Our records show a second identical charge from the same merchant within "
                               "{window_hours} hours with the same order reference. The duplicate is refunded.",
    "FP2_NO_DUPLICATE_FOUND": "Our records show no second charge of the same amount from this merchant within "
                              "{window_hours} hours, so no duplicate was found.",
    "FP3_CREDIT_ALREADY_POSTED": "Our records show the merchant has already credited this charge, "
                                 "so there is nothing further to refund.",
    "R0_MERCHANT_NO_TIMELY_RESPONSE": "The merchant did not respond within {merchant_days} days, "
                                      "so the disputed amount is refunded.",
    "MA_MERCHANT_ACCEPTED": "The merchant accepted the dispute. The disputed amount is refunded.",
    "CW_CARDMEMBER_WITHDREW": "The cardmember withdrew the dispute. The charge stands.",
    "R1_AMOUNT_ABOVE_AUTO_LIMIT": "Disputes above {max_auto} are always decided by a person.",
    "R2_NON_BASE_CURRENCY": "Disputes in currencies other than {base_currency} are decided by a person.",
    # the flag itself (for example the filer's dispute history) is shown only to staff, never in this sentence
    "R3_RISK_FLAG": "This dispute was routed to a person for an additional review.",
    "R4_MERCHANT_EVIDENCE_STRONGER": "The merchant's evidence was clearly stronger: merchant {v_m_pct}% vs "
                                     "cardmember {v_cm_pct}%. Merchant evidence considered: {merchant_items}. "
                                     "The charge stands.",
    "R5_CARDMEMBER_EVIDENCE_STRONGER": "The cardmember's evidence was clearly stronger: cardmember {v_cm_pct}% vs "
                                       "merchant {v_m_pct}%. Cardmember evidence considered: {cardmember_items}. "
                                       "The disputed amount is refunded.",
    "R6_SETTLEMENT_OFFER": "The evidence was close (merchant {v_m_pct}% vs cardmember {v_cm_pct}%). "
                           "Both parties are offered a split: {refund} refunded to the cardmember. "
                           "If either party declines, a person decides.",
    "R7_AMBIGUOUS": "The evidence was close, so a person will decide.",
    "OA_OFFER_ACCEPTED": "Both parties accepted the settlement offer of {refund}.",
    "HR_REVIEWER_DECISION": "A reviewer decided this dispute: {reviewer_rationale}",
    "OFFER_DECLINED": "The settlement offer was declined, so a person will decide.",
    "OFFER_EXPIRED": "The settlement offer was not accepted by both parties within {offer_days} days, "
                     "so a person will decide.",
    "APPEAL_BY_CARDMEMBER": "The cardmember appealed the automated decision. A person will review it.",
    "APPEAL_BY_MERCHANT": "The merchant appealed the automated decision. A person will review it.",
}


def explain(rule_id, values):
    # fill the template for this rule; a missing placeholder is a programming error
    return TEMPLATES[rule_id].format(**values)
