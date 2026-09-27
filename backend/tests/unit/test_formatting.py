# T-UNIT-FMT-01: display helpers used in explanations (integer money formatting, percentages, dates, labels).
from datetime import datetime, timezone
from fractions import Fraction

from tests.paths import POLICY_PATH
from app.domain.formatting import format_inr, format_money, humanize, items_text, local_date_text, pct_text
from app.domain.policy import load_policy

POLICY = load_policy(POLICY_PATH)


def test_indian_digit_grouping_with_integer_arithmetic():
    assert format_inr(0) == "₹0.00"
    assert format_inr(5) == "₹0.05"
    assert format_inr(499950) == "₹4,999.50"
    assert format_inr(12345678) == "₹1,23,456.78"
    assert format_inr(1234567890) == "₹1,23,45,678.90"
    assert format_inr(-250000) == "-₹2,500.00"


def test_other_currencies_use_the_code():
    assert format_money(499900, "INR") == "₹4,999.00"
    assert format_money(123456, "USD") == "USD 1234.56"


def test_percentages_from_exact_fractions():
    assert pct_text(Fraction(9, 10)) == "90"
    assert pct_text(Fraction(45, 1000)) == "4.5"
    assert pct_text(Fraction(85675, 100000)) == "85.68"      # 8567.5 bp rounds half to even
    assert pct_text(Fraction(-2610, 100000)) == "-2.61"


def test_dates_in_the_policy_time_zone():
    late_evening_utc = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)   # 01:30 on the 27th in IST
    assert local_date_text(late_evening_utc, "Asia/Kolkata") == "27 Sep 2026"


def test_evidence_lists_for_explanations():
    evidence = [{"evidence_type": "carrier_delivery_confirmation", "source": "system_verified"},
                {"evidence_type": "carrier_delivery_confirmation", "source": "system_verified"},
                {"evidence_type": "written_statement", "source": "self_attested"},
                {"evidence_type": "item_photos", "source": "document"}]           # not a C08 type: ignored
    assert humanize("signed_proof_of_delivery") == "signed proof of delivery"
    assert items_text(evidence, "MERCHANT", "C08", POLICY) == "carrier delivery confirmation (verified by system)"
    assert items_text(evidence, "CARDMEMBER", "C08", POLICY) == "written statement (statement)"
    assert items_text([], "MERCHANT", "C08", POLICY) == "none"
