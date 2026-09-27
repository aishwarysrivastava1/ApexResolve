"""Load and validate the decision policy (policy/policy.<version>.yaml).

The policy file is the single source of truth for every number the engine uses.
Loading fails loudly if anything is missing or has the wrong type.
"""
import yaml

REASON_CODES = ["C08", "C31", "C02", "P08"]
SIDES = ["MERCHANT", "CARDMEMBER"]
SOURCES = ["system_verified", "document", "self_attested"]
PRODUCED_BY = ["file", "text", "tracking"]


def load_policy(path):
    # read the YAML file into a plain Python dict
    with open(path, "r", encoding="utf-8") as handle:
        policy = yaml.safe_load(handle)
    validate_policy(policy)
    return policy


def require_int(value, name):
    # every number in the policy must be a whole number (no floats anywhere)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"policy field {name} must be an integer, got {value!r}")


def validate_policy(policy):
    # top-level keys that must exist
    for key in ["policy_version", "base_currency", "timezone", "windows", "anchor_date", "thresholds",
                "limits", "risk", "duplicate_check", "source_reliability_bp", "evidence_catalogue"]:
        if key not in policy:
            raise ValueError(f"policy is missing {key}")
    # all numeric sections contain integers only
    for section in ["windows", "thresholds", "limits", "risk", "duplicate_check", "source_reliability_bp"]:
        for name, value in policy[section].items():
            require_int(value, f"{section}.{name}")
    # every reliability is present and between 0 and 10000
    for source in SOURCES:
        value = policy["source_reliability_bp"][source]
        if value < 0 or value > 10000:
            raise ValueError(f"reliability for {source} out of range")
    # thresholds must leave an ambiguous zone around zero
    if not policy["thresholds"]["cardmember_win_margin_bp"] < 0 < policy["thresholds"]["merchant_win_margin_bp"]:
        raise ValueError("thresholds must satisfy cardmember_win < 0 < merchant_win")
    # every supported reason code has an anchor rule and a catalogue
    for code in REASON_CODES:
        if code not in policy["anchor_date"]:
            raise ValueError(f"anchor_date missing for {code}")
        if code not in policy["evidence_catalogue"]:
            raise ValueError(f"evidence_catalogue missing for {code}")
        for evidence_type, entry in policy["evidence_catalogue"][code].items():
            if entry["side"] not in SIDES:
                raise ValueError(f"{code}.{evidence_type}: bad side")
            if entry["produced_by"] not in PRODUCED_BY:
                raise ValueError(f"{code}.{evidence_type}: bad produced_by")
            require_int(entry["weight_bp"], f"{code}.{evidence_type}.weight_bp")
            if entry["weight_bp"] < 0 or entry["weight_bp"] > 10000:
                raise ValueError(f"{code}.{evidence_type}: weight out of range")
            if entry["compelling"] and entry["side"] != "MERCHANT":
                raise ValueError(f"{code}.{evidence_type}: only merchant items can be compelling")
