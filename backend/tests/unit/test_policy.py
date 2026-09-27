# T-UNIT-POL-01: the policy loader refuses every kind of broken policy file (fail fast at startup).
import copy

import pytest

from tests.paths import POLICY_PATH
from app.domain.policy import load_policy, validate_policy

GOOD = load_policy(POLICY_PATH)


def broken(change):
    # apply one small change to a deep copy of the real policy
    policy = copy.deepcopy(GOOD)
    change(policy)
    return policy


def drop_limits(p):
    del p["limits"]


def float_threshold(p):
    p["thresholds"]["merchant_win_margin_bp"] = 0.4


def bool_limit(p):
    p["limits"]["max_open_disputes_per_cardmember"] = True


def reliability_too_high(p):
    p["source_reliability_bp"]["document"] = 10001


def thresholds_without_gap(p):
    p["thresholds"]["cardmember_win_margin_bp"] = 100


def no_anchor_for_p08(p):
    del p["anchor_date"]["P08"]


def no_catalogue_for_c02(p):
    del p["evidence_catalogue"]["C02"]


def bad_side(p):
    p["evidence_catalogue"]["C08"]["merchant_statement"]["side"] = "BANK"


def bad_producer(p):
    p["evidence_catalogue"]["C08"]["merchant_statement"]["produced_by"] = "email"


def weight_too_high(p):
    p["evidence_catalogue"]["C08"]["merchant_statement"]["weight_bp"] = 20000


def compelling_cardmember_item(p):
    p["evidence_catalogue"]["C08"]["written_statement"]["compelling"] = True


BROKEN = [drop_limits, float_threshold, bool_limit, reliability_too_high, thresholds_without_gap,
          no_anchor_for_p08, no_catalogue_for_c02, bad_side, bad_producer, weight_too_high, compelling_cardmember_item]


def test_the_real_policy_is_valid():
    validate_policy(copy.deepcopy(GOOD))


@pytest.mark.parametrize("change", BROKEN, ids=[f.__name__ for f in BROKEN])
def test_broken_policy_is_refused(change):
    with pytest.raises(ValueError):
        validate_policy(broken(change))
