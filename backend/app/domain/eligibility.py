"""Filing-window and fact checks. Pure: every input is passed in, nothing is read from a clock or database."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def anchor_date(reason_code, transaction_at, expected_delivery_date, policy):
    # the calendar date (in the policy timezone) that the filing window is counted from
    zone = ZoneInfo(policy["timezone"])
    transaction_local_date = transaction_at.astimezone(zone).date()
    rule = policy["anchor_date"][reason_code]
    if rule == "transaction_date":
        return transaction_local_date
    if rule == "later_of_transaction_or_expected_delivery":
        if expected_delivery_date is not None and expected_delivery_date > transaction_local_date:
            return expected_delivery_date
        return transaction_local_date
    raise ValueError(f"unknown anchor rule {rule}")


def filing_deadline(reason_code, transaction_at, expected_delivery_date, policy):
    # last moment a claim is accepted: end of day (anchor + window) in the policy timezone
    zone = ZoneInfo(policy["timezone"])
    start = anchor_date(reason_code, transaction_at, expected_delivery_date, policy)
    last_day = start + timedelta(days=policy["windows"]["filing_window_days"])
    next_midnight_local = datetime.combine(last_day + timedelta(days=1), time(0, 0), tzinfo=zone)
    return next_midnight_local  # claims strictly before this instant are on time


def is_within_filing_window(claim_received_at, reason_code, transaction_at, expected_delivery_date, policy):
    deadline = filing_deadline(reason_code, transaction_at, expected_delivery_date, policy)
    return claim_received_at < deadline


def duplicate_check(disputed, other_charges, credits, policy):
    """P08 fact check using Amex's own records.

    disputed / other_charges: dicts with transaction_id, account_token, se_number, amount_minor,
    currency, transaction_at, order_ref. credits: CREDIT transactions with original_transaction_id.
    Returns CREDIT_ALREADY_POSTED | CONFIRMED | INCONCLUSIVE | NOT_FOUND.
    """
    window = timedelta(hours=policy["duplicate_check"]["window_hours"])
    similar = []
    for other in other_charges:
        if other["transaction_id"] == disputed["transaction_id"]:
            continue
        same_money = (other["amount_minor"] == disputed["amount_minor"]
                      and other["currency"] == disputed["currency"])
        same_parties = (other["account_token"] == disputed["account_token"]
                        and other["se_number"] == disputed["se_number"])
        close_in_time = abs(other["transaction_at"] - disputed["transaction_at"]) <= window
        if same_money and same_parties and close_in_time:
            similar.append(other)
    if len(similar) == 0:
        return "NOT_FOUND"
    # if the merchant already refunded either charge, there is nothing left to charge back
    involved_ids = [disputed["transaction_id"]] + [other["transaction_id"] for other in similar]
    for credit in credits:
        if credit["original_transaction_id"] in involved_ids and credit["amount_minor"] >= disputed["amount_minor"]:
            return "CREDIT_ALREADY_POSTED"
    for other in similar:
        both_missing = other["order_ref"] is None and disputed["order_ref"] is None
        same_order = other["order_ref"] is not None and other["order_ref"] == disputed["order_ref"]
        if both_missing or same_order:
            return "CONFIRMED"
    # a similar charge exists but with a different order reference: evidence decides
    return "INCONCLUSIVE"


def credit_check(disputed, credits, disputed_amount_minor):
    """C02 fact check: has the merchant already submitted a credit for this charge?"""
    total = 0
    for credit in credits:
        if credit["original_transaction_id"] == disputed["transaction_id"]:
            total = total + credit["amount_minor"]
    if total >= disputed_amount_minor:
        return "CREDIT_ALREADY_POSTED"
    if total > 0:
        return "PARTIAL_CREDIT_FOUND"
    return "NO_CREDIT_FOUND"


# fact-check result -> (verdict, rule_id) for the filing fast path; results not listed use the normal flow
FAST_PATH = {
    ("P08", "CONFIRMED"): ("CARDMEMBER_REFUND", "FP1_DUPLICATE_CONFIRMED"),
    ("P08", "NOT_FOUND"): ("MERCHANT_UPHELD", "FP2_NO_DUPLICATE_FOUND"),
    ("P08", "CREDIT_ALREADY_POSTED"): ("MERCHANT_UPHELD", "FP3_CREDIT_ALREADY_POSTED"),
    ("C02", "CREDIT_ALREADY_POSTED"): ("MERCHANT_UPHELD", "FP3_CREDIT_ALREADY_POSTED"),
}


def fast_path(reason_code, fact_result, disputed_amount_minor, currency, policy):
    # the fast path only applies to small, base-currency disputes with a conclusive fact
    if disputed_amount_minor > policy["limits"]["max_auto_amount_minor"]:
        return None
    if currency != policy["base_currency"]:
        return None
    return FAST_PATH.get((reason_code, fact_result))


def carrier_result_to_evidence(submission_kind, carrier_status, delivered_postcode, expected_postcode):
    """Map a mock-carrier lookup to (evidence_type, source).

    submission_kind: shipment_tracking (merchant, C08) or return_tracking (cardmember, C31/C02).
    carrier_status: DELIVERED | IN_TRANSIT | NOT_DELIVERED | RETURNED_TO_SENDER | NOT_FOUND.
    """
    if submission_kind == "shipment_tracking":
        if carrier_status == "NOT_FOUND":
            return ("carrier_delivery_confirmation", "self_attested")
        if carrier_status == "DELIVERED" and delivered_postcode == expected_postcode:
            return ("carrier_delivery_confirmation", "system_verified")
        if carrier_status == "DELIVERED":
            return ("carrier_wrong_address", "system_verified")
        return ("carrier_not_delivered", "system_verified")
    if submission_kind == "return_tracking":
        if carrier_status == "DELIVERED" and delivered_postcode == expected_postcode:
            return ("return_shipment", "system_verified")
        return ("return_shipment", "self_attested")
    raise ValueError(f"unknown submission kind {submission_kind}")
