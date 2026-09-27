# Illustrative threshold calibration on SYNTHETIC C08 disputes (assumptions below are made up;
# replace them with labelled historical outcomes before trusting any number).
import os
import sys
import random
import copy
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)
from app.domain.policy import load_policy  # noqa: E402
from app.domain.decision import decide  # noqa: E402

BASE = load_policy(os.path.join(os.path.dirname(BACKEND), "policy", "policy.2026.09.1.yaml"))
STMT_CM = {"evidence_type": "written_statement", "source": "self_attested"}
STMT_M = {"evidence_type": "merchant_statement", "source": "self_attested"}


def synth_case(rng):
    merchant_right = rng.random() < 0.5
    ev = [STMT_CM]
    if rng.random() < 0.9:
        ev.append(STMT_M)
    if merchant_right:
        if rng.random() < 0.7:  # merchant gives tracking
            roll = rng.random()
            ev.append({"evidence_type": "carrier_delivery_confirmation",
                       "source": "system_verified" if roll < 0.95 else "self_attested"})
        elif rng.random() < 0.4:
            ev.append({"evidence_type": "signed_proof_of_delivery", "source": "document"})
        if rng.random() < 0.2:
            ev.append({"evidence_type": "merchant_correspondence", "source": "document"})
    else:
        if rng.random() < 0.5:
            roll = rng.random()
            if roll < 0.6:
                ev.append({"evidence_type": "carrier_not_delivered", "source": "system_verified"})
            elif roll < 0.9:
                ev.append({"evidence_type": "carrier_wrong_address", "source": "system_verified"})
            else:
                ev.append({"evidence_type": "carrier_delivery_confirmation", "source": "self_attested"})
        if rng.random() < 0.1:
            ev.append({"evidence_type": "signed_proof_of_delivery", "source": "document"})
        if rng.random() < 0.5:
            ev.append({"evidence_type": "merchant_correspondence", "source": "document"})
    amount = rng.choice([49900, 99900, 199900, 499900, 999900, 1999900])
    return merchant_right, {"reason_code": "C08", "disputed_amount_minor": amount, "currency": "INR",
                            "risk_flag": None, "evidence": ev}


def run(theta_bp, cases, cost_error=10, cost_review=1):
    policy = copy.deepcopy(BASE)
    policy["thresholds"]["merchant_win_margin_bp"] = theta_bp
    policy["thresholds"]["cardmember_win_margin_bp"] = -theta_bp
    auto = errors = offers = reviews = 0
    for truth_merchant, case in cases:
        r = decide(case, policy)
        if r["outcome"] == "DECIDED":
            auto += 1
            if (r["verdict"] == "MERCHANT_UPHELD") != truth_merchant:
                errors += 1
        elif r["outcome"] == "SETTLEMENT_OFFERED":
            offers += 1
        else:
            reviews += 1
    n = len(cases)
    # offers that are declined become reviews; assume half are declined
    expected_reviews = reviews + offers / 2
    cost = cost_error * errors + cost_review * expected_reviews
    return {"theta": theta_bp / 10000, "auto_rate": auto / n, "auto_error_rate": errors / max(auto, 1),
            "offer_rate": offers / n, "review_rate": reviews / n, "cost_per_case": cost / n}


rng = random.Random(20260926)
cases = [synth_case(rng) for _ in range(20000)]
print("| theta | automated | error rate among automated | offered | human review | expected cost / case |")
print("|---|---|---|---|---|---|")
for theta in [1000, 2000, 3000, 4000, 5000, 6000]:
    r = run(theta, cases)
    print(f"| {r['theta']:.2f} | {r['auto_rate']:.1%} | {r['auto_error_rate']:.2%} | {r['offer_rate']:.1%} | {r['review_rate']:.1%} | {r['cost_per_case']:.3f} |")
