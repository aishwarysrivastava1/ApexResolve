"""Pure decision engine. No database, no network, no clock.

All arithmetic uses fractions.Fraction, so results are exact and every
comparison with a threshold is exact (no floating-point surprises).
"""
from fractions import Fraction

BP = 10000  # basis points in 1.0


def item_strength(weight_bp, source, policy):
    # strength of one item = catalogue weight x reliability of how it was obtained
    reliability_bp = policy["source_reliability_bp"][source]
    return Fraction(weight_bp, BP) * Fraction(reliability_bp, BP)


def side_strength(evidence, side, reason_code, policy):
    # combine every relevant item that supports one side into a value in [0, 1)
    catalogue = policy["evidence_catalogue"][reason_code]
    best_per_type = {}
    for item in evidence:
        evidence_type = item["evidence_type"]
        # items not in this reason code's catalogue are ignored
        if evidence_type not in catalogue:
            continue
        entry = catalogue[evidence_type]
        # items that support the other side are ignored here
        if entry["side"] != side:
            continue
        strength = item_strength(entry["weight_bp"], item["source"], policy)
        # the same evidence type counts once: keep only its strongest copy
        if strength > best_per_type.get(evidence_type, Fraction(0)):
            best_per_type[evidence_type] = strength
    # noisy-OR: 1 minus the chance that every distinct item is wrong
    remaining_doubt = Fraction(1)
    for strength in best_per_type.values():
        remaining_doubt = remaining_doubt * (1 - strength)
    return 1 - remaining_doubt


def has_compelling_merchant_item(evidence, reason_code, policy):
    # a compelling item must be marked compelling AND not merely self-attested
    catalogue = policy["evidence_catalogue"][reason_code]
    for item in evidence:
        entry = catalogue.get(item["evidence_type"])
        if entry is None:
            continue
        if entry["side"] == "MERCHANT" and entry["compelling"] and item["source"] != "self_attested":
            return True
    return False


def offer_amount(disputed_amount_minor, margin):
    # cardmember's share of the amount = (1 - margin) / 2, rounded half-to-even to whole minor units
    share = (1 - margin) / 2
    return round(disputed_amount_minor * share)


def decide(case, policy):
    """Run the ordered decision table R1..R7 on a case that is READY_FOR_DECISION.

    case = {reason_code, disputed_amount_minor, currency, risk_flag, evidence: [{evidence_type, source}]}
    Returns a dict with outcome (DECIDED | SETTLEMENT_OFFERED | HUMAN_REVIEW), verdict, rule_id,
    v_m, v_cm, margin (exact Fractions) and refund_amount_minor.
    """
    limits = policy["limits"]
    thresholds = policy["thresholds"]
    amount = case["disputed_amount_minor"]
    code = case["reason_code"]
    # the evidence values are computed for every outcome so they can be logged and explained
    v_m = side_strength(case["evidence"], "MERCHANT", code, policy)
    v_cm = side_strength(case["evidence"], "CARDMEMBER", code, policy)
    margin = v_m - v_cm
    result = {"v_m": v_m, "v_cm": v_cm, "margin": margin, "verdict": None, "refund_amount_minor": None}
    merchant_win = Fraction(thresholds["merchant_win_margin_bp"], BP)
    cardmember_win = Fraction(thresholds["cardmember_win_margin_bp"], BP)
    # R1: large amounts always go to a human
    if amount > limits["max_auto_amount_minor"]:
        result.update(outcome="HUMAN_REVIEW", rule_id="R1_AMOUNT_ABOVE_AUTO_LIMIT")
        return result
    # R2: only the base currency is decided automatically
    if case["currency"] != policy["base_currency"]:
        result.update(outcome="HUMAN_REVIEW", rule_id="R2_NON_BASE_CURRENCY")
        return result
    # R3: any risk flag goes to a human (never an automatic denial)
    if case["risk_flag"] is not None:
        result.update(outcome="HUMAN_REVIEW", rule_id="R3_RISK_FLAG")
        return result
    # R4: clear merchant win needs the margin AND at least one compelling merchant item
    if margin >= merchant_win and has_compelling_merchant_item(case["evidence"], code, policy):
        result.update(outcome="DECIDED", rule_id="R4_MERCHANT_EVIDENCE_STRONGER",
                      verdict="MERCHANT_UPHELD", refund_amount_minor=0)
        return result
    # R5: clear cardmember win
    if margin <= cardmember_win:
        result.update(outcome="DECIDED", rule_id="R5_CARDMEMBER_EVIDENCE_STRONGER",
                      verdict="CARDMEMBER_REFUND", refund_amount_minor=amount)
        return result
    # R6: ambiguous and small -> offer a split that both parties must accept
    if limits["min_offer_amount_minor"] <= amount <= limits["max_offer_amount_minor"]:
        result.update(outcome="SETTLEMENT_OFFERED", rule_id="R6_SETTLEMENT_OFFER",
                      refund_amount_minor=offer_amount(amount, margin))
        return result
    # R7: everything else is decided by a human
    result.update(outcome="HUMAN_REVIEW", rule_id="R7_AMBIGUOUS")
    return result


def fraction_to_decimal_string(value):
    # all values here have denominators made of 2s and 5s, so the decimal expansion ends
    numerator = value.numerator
    denominator = value.denominator
    sign = "-" if numerator < 0 else ""
    numerator = abs(numerator)
    whole = numerator // denominator
    remainder = numerator % denominator
    digits = ""
    count = 0
    while remainder != 0:
        remainder = remainder * 10
        digits = digits + str(remainder // denominator)
        remainder = remainder % denominator
        count = count + 1
        if count > 200:
            raise ValueError("value does not have a terminating decimal expansion")
    if digits == "":
        return f"{sign}{whole}"
    return f"{sign}{whole}.{digits}"


def to_bp(value):
    # display/storage helper: round half-to-even to whole basis points
    return round(value * BP)
