from fractions import Fraction
from hypothesis import given, settings, strategies as st
from app.domain.decision import decide, side_strength, offer_amount, fraction_to_decimal_string
from app.domain.policy import load_policy, SOURCES
from tests.paths import POLICY_PATH

POLICY = load_policy(POLICY_PATH)
OUTCOMES = {"DECIDED", "SETTLEMENT_OFFERED", "HUMAN_REVIEW"}
RANK = {"CARDMEMBER_REFUND": 0, "SETTLEMENT_OFFERED": 1, "HUMAN_REVIEW": 1, "MERCHANT_UPHELD": 2}


def item_strategy(code):
    types = sorted(POLICY["evidence_catalogue"][code].keys())
    return st.fixed_dictionaries({"evidence_type": st.sampled_from(types + ["unknown_type"]),
                                  "source": st.sampled_from(SOURCES)})


def case_strategy():
    return st.sampled_from(["C08", "C31", "C02", "P08"]).flatmap(lambda code: st.fixed_dictionaries({
        "reason_code": st.just(code),
        "disputed_amount_minor": st.integers(min_value=1, max_value=8_000_000),
        "currency": st.sampled_from(["INR", "INR", "INR", "USD"]),
        "risk_flag": st.sampled_from([None, None, None, "HIGH_DISPUTE_VELOCITY"]),
        "evidence": st.lists(item_strategy(code), max_size=12),
    }))


def rank_of(result):
    if result["outcome"] == "DECIDED":
        return RANK[result["verdict"]]
    return RANK[result["outcome"]]


@settings(max_examples=3000, deadline=None)
@given(case_strategy())
def test_always_exactly_one_valid_outcome(case):
    result = decide(case, POLICY)
    assert result["outcome"] in OUTCOMES
    assert 0 <= result["v_m"] < 1 and 0 <= result["v_cm"] < 1
    if result["outcome"] == "DECIDED":
        assert result["verdict"] in ("MERCHANT_UPHELD", "CARDMEMBER_REFUND")
    if result["outcome"] == "SETTLEMENT_OFFERED":
        assert 1 <= result["refund_amount_minor"] <= case["disputed_amount_minor"] - 1


@settings(max_examples=3000, deadline=None)
@given(case_strategy(), st.data())
def test_more_merchant_evidence_never_hurts_merchant(case, data):
    before = decide(case, POLICY)
    extra = data.draw(item_strategy(case["reason_code"]))
    entry = POLICY["evidence_catalogue"][case["reason_code"]].get(extra["evidence_type"])
    if entry is None or entry["side"] != "MERCHANT":
        return
    after = decide(dict(case, evidence=case["evidence"] + [extra]), POLICY)
    assert rank_of(after) >= rank_of(before)


@settings(max_examples=3000, deadline=None)
@given(case_strategy(), st.data())
def test_more_cardmember_evidence_never_hurts_cardmember(case, data):
    before = decide(case, POLICY)
    extra = data.draw(item_strategy(case["reason_code"]))
    entry = POLICY["evidence_catalogue"][case["reason_code"]].get(extra["evidence_type"])
    if entry is None or entry["side"] != "CARDMEMBER":
        return
    after = decide(dict(case, evidence=case["evidence"] + [extra]), POLICY)
    assert rank_of(after) <= rank_of(before)


@settings(max_examples=2000, deadline=None)
@given(case_strategy(), st.integers(min_value=2, max_value=10))
def test_repeating_an_item_changes_nothing(case, copies):
    if not case["evidence"]:
        return
    repeated = dict(case, evidence=case["evidence"] + [case["evidence"][0]] * copies)
    assert decide(repeated, POLICY) == decide(case, POLICY)


@settings(max_examples=2000, deadline=None)
@given(case_strategy())
def test_order_of_items_does_not_matter(case):
    reversed_case = dict(case, evidence=list(reversed(case["evidence"])))
    assert decide(reversed_case, POLICY) == decide(case, POLICY)


@settings(max_examples=2000, deadline=None)
@given(st.integers(min_value=200, max_value=1_000_000), st.integers(min_value=-3999, max_value=3999))
def test_offer_shares_are_complementary_and_bounded(amount, margin_bp):
    margin = Fraction(margin_bp, 10000)
    refund = offer_amount(amount, margin)
    assert 1 <= refund <= amount - 1
    # swapping the sides (negating the margin) gives the complementary share, up to half-even rounding
    assert abs((amount - offer_amount(amount, -margin)) - refund) <= 1


def test_values_have_exact_decimal_strings():
    v = side_strength([{"evidence_type": "carrier_delivery_confirmation", "source": "self_attested"},
                       {"evidence_type": "signed_proof_of_delivery", "source": "document"}], "MERCHANT", "C08", POLICY)
    assert fraction_to_decimal_string(v) == "0.6788"


def with_thresholds(merchant_bp, cardmember_bp):
    # a copy of the policy with other thresholds (the real file is never changed by tests)
    policy = dict(POLICY)
    policy["thresholds"] = dict(POLICY["thresholds"], merchant_win_margin_bp=merchant_bp,
                                cardmember_win_margin_bp=cardmember_bp)
    return policy


def test_boundaries_are_exact():
    # T-UNIT-DEC-01: a margin exactly on a threshold is decided (>= and <=); one basis point inside is not
    merchant_case = {"reason_code": "C08", "disputed_amount_minor": 1000, "currency": "INR", "risk_flag": None,
                     "evidence": [{"evidence_type": "signed_proof_of_delivery", "source": "document"}]}
    assert decide(merchant_case, POLICY)["margin"] == Fraction(56, 100)             # 0.8 x 0.7
    assert decide(merchant_case, with_thresholds(5600, -4000))["rule_id"] == "R4_MERCHANT_EVIDENCE_STRONGER"
    assert decide(merchant_case, with_thresholds(5601, -4000))["rule_id"] == "R6_SETTLEMENT_OFFER"
    cardmember_case = {"reason_code": "C08", "disputed_amount_minor": 1000, "currency": "INR", "risk_flag": None,
                       "evidence": [{"evidence_type": "carrier_not_delivered", "source": "system_verified"}]}
    assert decide(cardmember_case, POLICY)["margin"] == Fraction(-9, 10)
    assert decide(cardmember_case, with_thresholds(4000, -9000))["rule_id"] == "R5_CARDMEMBER_EVIDENCE_STRONGER"
    assert decide(cardmember_case, with_thresholds(4000, -8999))["rule_id"] == "R5_CARDMEMBER_EVIDENCE_STRONGER"
    assert decide(cardmember_case, with_thresholds(4000, -9001))["rule_id"] == "R6_SETTLEMENT_OFFER"


def test_compelling_guard_blocks_merchant_win_without_compelling_item():
    # with the shipped catalogue non-compelling merchant items cannot reach +0.40, so test the guard
    # on a policy variant with a lower threshold: margin >= threshold but nothing compelling -> no merchant win
    import copy
    variant = copy.deepcopy(POLICY)
    variant["thresholds"]["merchant_win_margin_bp"] = 2000
    case = {"reason_code": "C08", "disputed_amount_minor": 499900, "currency": "INR", "risk_flag": None,
            "evidence": [{"evidence_type": "carrier_delivery_confirmation", "source": "self_attested"},
                         {"evidence_type": "merchant_statement", "source": "self_attested"}]}
    result = decide(case, variant)
    assert result["margin"] >= Fraction(2000, 10000)
    assert result["outcome"] == "SETTLEMENT_OFFERED"
