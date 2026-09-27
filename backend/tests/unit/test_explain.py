# T-UNIT-EXPL-01: every rule id and status reason has exactly one fixed template, and every template can be
# filled from the values the services provide (no missing placeholder, no leftover braces, no free text).
import re
import string

from app.domain.explain import TEMPLATES, explain

RULES = ["E1_FILING_WINDOW_EXPIRED", "FP1_DUPLICATE_CONFIRMED", "FP2_NO_DUPLICATE_FOUND", "FP3_CREDIT_ALREADY_POSTED",
         "R0_MERCHANT_NO_TIMELY_RESPONSE", "MA_MERCHANT_ACCEPTED", "CW_CARDMEMBER_WITHDREW",
         "R1_AMOUNT_ABOVE_AUTO_LIMIT", "R2_NON_BASE_CURRENCY", "R3_RISK_FLAG", "R4_MERCHANT_EVIDENCE_STRONGER",
         "R5_CARDMEMBER_EVIDENCE_STRONGER", "R6_SETTLEMENT_OFFER", "R7_AMBIGUOUS", "OA_OFFER_ACCEPTED",
         "HR_REVIEWER_DECISION"]
STATUS_REASONS = ["OFFER_DECLINED", "OFFER_EXPIRED", "APPEAL_BY_CARDMEMBER", "APPEAL_BY_MERCHANT"]

# the placeholder values services.disputes.explanation_values produces (shapes, not real data)
VALUES = {"deadline_local": "20 Jan 2027", "window_days": 120, "anchor_label": "the expected delivery date",
          "window_hours": 72, "merchant_days": 20, "max_auto": "₹50,000.00", "base_currency": "INR",
          "merchant_items": "signed proof of delivery (document)",
          "cardmember_items": "written statement (statement)", "v_m_pct": "56", "v_cm_pct": "4.5",
          "refund": "₹2,499.50", "reviewer_rationale": "Both sides were credible.", "offer_days": 5}


def placeholders(template):
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


def test_one_template_per_rule_and_status_reason():
    assert set(TEMPLATES) == set(RULES) | set(STATUS_REASONS)


def test_every_template_fills_completely():
    for rule_id, template in TEMPLATES.items():
        assert placeholders(template) <= set(VALUES), (rule_id, placeholders(template) - set(VALUES))
        text = explain(rule_id, VALUES)
        assert "{" not in text and "}" not in text, rule_id
        assert re.search(r"[.]$", text), rule_id        # a complete sentence


def test_evidence_decisions_state_both_scores():
    text = explain("R4_MERCHANT_EVIDENCE_STRONGER", VALUES)
    assert "merchant 56%" in text and "cardmember 4.5%" in text and "signed proof of delivery" in text


def test_risk_flag_never_appears_in_party_text():
    # R3's explanation goes to both parties (status_explanation, notifications); the flag is staff-only
    text = explain("R3_RISK_FLAG", VALUES)
    assert "velocity" not in text.lower() and "{" not in TEMPLATES["R3_RISK_FLAG"]
