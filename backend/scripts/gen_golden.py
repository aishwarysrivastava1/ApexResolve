# Generates the golden decision vectors (JSON + Markdown table) from the reference engine.
import json
import os
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)
from app.domain.policy import load_policy  # noqa: E402
from app.domain.decision import decide, fraction_to_decimal_string, to_bp  # noqa: E402

P = load_policy(os.path.join(os.path.dirname(BACKEND), "policy", "policy.2026.09.1.yaml"))
STMT = {"evidence_type": "written_statement", "source": "self_attested"}
M_STMT = {"evidence_type": "merchant_statement", "source": "self_attested"}


def ev(t, s):
    return {"evidence_type": t, "source": s}


CASES = [
    ("G01", "C08: carrier API confirms delivery to the cardmember's postcode", "C08", 499900, "INR", None,
     [ev("carrier_delivery_confirmation", "system_verified"), STMT]),
    ("G02", "C08: signed proof of delivery uploaded", "C08", 499900, "INR", None,
     [ev("signed_proof_of_delivery", "document"), STMT]),
    ("G03", "C08: carrier API says not delivered", "C08", 499900, "INR", None,
     [ev("carrier_not_delivered", "system_verified"), M_STMT, STMT]),
    ("G04", "C08: carrier delivered to a different postcode; merchant uploads signed POD", "C08", 499900, "INR", None,
     [ev("carrier_wrong_address", "system_verified"), ev("signed_proof_of_delivery", "document"), STMT]),
    ("G05", "C08: statements only on both sides (small amount)", "C08", 499900, "INR", None,
     [M_STMT, STMT]),
    ("G06", "C08: statements only, amount above the offer limit", "C08", 2000000, "INR", None,
     [M_STMT, STMT]),
    ("G07", "C08: amount above the automation limit", "C08", 6000000, "INR", None,
     [ev("carrier_delivery_confirmation", "system_verified"), STMT]),
    ("G08", "C08: non-base currency", "C08", 499900, "USD", None,
     [ev("carrier_delivery_confirmation", "system_verified"), STMT]),
    ("G09", "C08: dispute-velocity risk flag", "C08", 499900, "INR", "HIGH_DISPUTE_VELOCITY",
     [ev("carrier_delivery_confirmation", "system_verified"), STMT]),
    ("G10", "C08: unverifiable tracking vs cardmember correspondence", "C08", 499900, "INR", None,
     [ev("carrier_delivery_confirmation", "self_attested"), M_STMT, ev("merchant_correspondence", "document"), STMT]),
    ("G11", "C31: listing proof vs cardmember photos", "C31", 349900, "INR", None,
     [ev("description_match_proof", "document"), ev("item_photos", "document"), STMT]),
    ("G12", "C31: cardmember accepted the item in writing", "C31", 349900, "INR", None,
     [ev("cardmember_acceptance", "document"), ev("description_match_proof", "document"), STMT]),
    ("G13", "C31: verified return shipment plus photos", "C31", 349900, "INR", None,
     [ev("return_shipment", "system_verified"), ev("item_photos", "document"), M_STMT, STMT]),
    ("G14", "C02: credit promise vs refund-policy disclosure", "C02", 250000, "INR", None,
     [ev("credit_acknowledgment", "document"), ev("refund_policy_disclosure", "document"), STMT]),
    ("G15", "P08 (inconclusive fact check): merchant proves two distinct orders", "P08", 129900, "INR", None,
     [ev("distinct_orders_proof", "document"), STMT]),
    ("G16", "G05 with the merchant statement repeated five times (no stacking)", "C08", 499900, "INR", None,
     [M_STMT, M_STMT, M_STMT, M_STMT, M_STMT, STMT]),
    ("G17", "C31: only non-compelling merchant evidence (repair offer) vs cardmember statement", "C31", 349900, "INR", None,
     [ev("repair_or_replacement_offer", "document"), M_STMT, STMT]),
    ("G18", "C08: evidence that belongs to another reason code is ignored", "C08", 499900, "INR", None,
     [ev("cardmember_acceptance", "document"), M_STMT, STMT]),
]

rows = []
for cid, title, code, amount, cur, risk, evidence in CASES:
    case = {"reason_code": code, "disputed_amount_minor": amount, "currency": cur, "risk_flag": risk, "evidence": evidence}
    r = decide(case, P)
    rows.append({
        "id": cid, "title": title, "reason_code": code, "disputed_amount_minor": amount, "currency": cur,
        "risk_flag": risk, "evidence": evidence,
        "v_m": fraction_to_decimal_string(r["v_m"]), "v_cm": fraction_to_decimal_string(r["v_cm"]),
        "margin": fraction_to_decimal_string(r["margin"]), "margin_bp": to_bp(r["margin"]),
        "outcome": r["outcome"], "rule_id": r["rule_id"], "verdict": r["verdict"],
        "refund_amount_minor": r["refund_amount_minor"],
    })

with open(os.path.join(BACKEND, "tests", "golden", "golden_vectors.json"), "w") as f:
    json.dump(rows, f, indent=2)

lines = ["| ID | Scenario | Code | Amount (minor) | V_M | V_CM | Margin | Rule | Outcome | Verdict | Refund (minor) |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
for r in rows:
    amt = f"{r['disputed_amount_minor']} {r['currency']}"
    lines.append(f"| {r['id']} | {r['title']} | {r['reason_code']} | {amt} | {r['v_m']} | {r['v_cm']} | {r['margin']} | "
                 f"{r['rule_id']} | {r['outcome']} | {r['verdict'] or '—'} | {r['refund_amount_minor'] if r['refund_amount_minor'] is not None else '—'} |")
with open(os.path.join(BACKEND, "tests", "golden", "golden_vectors.md"), "w") as f:
    f.write("\n".join(lines) + "\n")

ev_lines = ["| ID | Evidence items (type · source) |", "|---|---|"]
for r in rows:
    items = "; ".join(f"{e['evidence_type']} · {e['source']}" for e in r["evidence"])
    ev_lines.append(f"| {r['id']} | {items} |")
with open(os.path.join(BACKEND, "tests", "golden", "golden_inputs.md"), "w") as f:
    f.write("\n".join(ev_lines) + "\n")
print("\n".join(lines))
