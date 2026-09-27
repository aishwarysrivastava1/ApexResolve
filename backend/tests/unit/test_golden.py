# T-GOLD-ALL: the committed golden vectors (Math §15) must be reproduced exactly by the engine and the
# policy file. A policy or engine change that moves any vector fails here until the vectors are
# regenerated (make golden) and the change is reviewed.
import json
import os

import pytest

from tests.paths import BACKEND, POLICY_PATH
from app.domain.decision import decide, fraction_to_decimal_string, to_bp
from app.domain.policy import load_policy

POLICY = load_policy(POLICY_PATH)
VECTORS = json.load(open(os.path.join(BACKEND, "tests", "golden", "golden_vectors.json")))


def test_there_are_eighteen_vectors_with_unique_ids():
    ids = [v["id"] for v in VECTORS]
    assert ids == [f"G{n:02d}" for n in range(1, 19)]


@pytest.mark.parametrize("vector", VECTORS, ids=[v["id"] for v in VECTORS])
def test_golden_vector(vector):
    case = {key: vector[key] for key in ("reason_code", "disputed_amount_minor", "currency", "risk_flag", "evidence")}
    result = decide(case, POLICY)
    assert result["outcome"] == vector["outcome"]
    assert result["rule_id"] == vector["rule_id"]
    assert result["verdict"] == vector["verdict"]
    assert result["refund_amount_minor"] == vector["refund_amount_minor"]
    assert fraction_to_decimal_string(result["v_m"]) == vector["v_m"]
    assert fraction_to_decimal_string(result["v_cm"]) == vector["v_cm"]
    assert fraction_to_decimal_string(result["margin"]) == vector["margin"]
    assert to_bp(result["margin"]) == vector["margin_bp"]


def test_policy_version_matches_file_name():
    # the policy_version inside the file is the one written into every decision
    assert POLICY_PATH.endswith(f"policy.{POLICY['policy_version']}.yaml")
