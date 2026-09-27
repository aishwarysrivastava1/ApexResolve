from datetime import datetime, date, timedelta, timezone
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app.domain import fsm
from app.domain.eligibility import (is_within_filing_window, filing_deadline, duplicate_check, credit_check,
                                  fast_path, carrier_result_to_evidence)
from app.domain.redact import redact, luhn_is_valid, verhoeff_is_valid
from app.domain import ledger_math as L


# ---------------- FSM ----------------
def test_every_transition_uses_known_states():
    for (src, _event), dst in fsm.TRANSITIONS.items():
        assert src in fsm.STATES and dst in fsm.STATES


def test_terminal_states_have_no_outgoing_transitions():
    for (src, _event) in fsm.TRANSITIONS:
        assert src not in fsm.TERMINAL_STATES


def test_every_non_terminal_state_is_reachable_and_can_reach_closed():
    reachable = set(fsm.INITIAL_STATES)
    changed = True
    while changed:
        changed = False
        for (src, _e), dst in fsm.TRANSITIONS.items():
            if src in reachable and dst not in reachable:
                reachable.add(dst)
                changed = True
    assert reachable == set(fsm.STATES)
    for state in fsm.STATES:
        if state in fsm.TERMINAL_STATES:
            continue
        seen = {state}
        frontier = [state]
        while frontier:
            current = frontier.pop()
            for (src, _e), dst in fsm.TRANSITIONS.items():
                if src == current and dst not in seen:
                    seen.add(dst)
                    frontier.append(dst)
        assert "CLOSED" in seen, state


def test_illegal_transition_raises():
    with pytest.raises(fsm.IllegalTransition):
        fsm.next_state("CLOSED", "APPEAL_FILED")
    with pytest.raises(fsm.IllegalTransition):
        fsm.next_state("AWAITING_MERCHANT", "AUTO_DECIDED")


# ---------------- eligibility ----------------
IST = timezone(timedelta(hours=5, minutes=30))


def test_filing_window_inclusive_last_day(policy):
    txn = datetime(2026, 1, 10, 22, 0, tzinfo=IST)
    last_ok = datetime(2026, 5, 10, 23, 59, 59, tzinfo=IST)   # 10 Jan + 120 days = 10 May
    too_late = datetime(2026, 5, 11, 0, 0, 0, tzinfo=IST)
    assert is_within_filing_window(last_ok, "C31", txn, None, policy)
    assert not is_within_filing_window(too_late, "C31", txn, None, policy)


def test_c08_window_counts_from_expected_delivery(policy):
    txn = datetime(2026, 1, 10, 12, 0, tzinfo=IST)
    claim = datetime(2026, 6, 1, 12, 0, tzinfo=IST)            # 142 days after the transaction
    assert not is_within_filing_window(claim, "C08", txn, None, policy)
    assert is_within_filing_window(claim, "C08", txn, date(2026, 2, 15), policy)


def test_anchor_uses_policy_timezone(policy):
    # 20:00 UTC on 10 Jan is already 11 Jan in India
    txn = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)
    deadline = filing_deadline("C31", txn, None, policy)
    assert deadline.date() == date(2026, 5, 12)


def charge(tid, when, order_ref="ORD-1", amount=49900, se="1234567890", account="acct_1"):
    return {"transaction_id": tid, "account_token": account, "se_number": se, "amount_minor": amount,
            "currency": "INR", "transaction_at": when, "order_ref": order_ref}


def test_duplicate_check_outcomes(policy):
    t0 = datetime(2026, 3, 1, 10, 0, tzinfo=IST)
    disputed = charge("T1", t0)
    assert duplicate_check(disputed, [disputed, charge("T2", t0 + timedelta(hours=2))], [], policy) == "CONFIRMED"
    assert duplicate_check(disputed, [disputed, charge("T2", t0 + timedelta(hours=2), order_ref="ORD-2")], [], policy) == "INCONCLUSIVE"
    assert duplicate_check(disputed, [disputed, charge("T2", t0 + timedelta(hours=73))], [], policy) == "NOT_FOUND"
    assert duplicate_check(disputed, [disputed, charge("T2", t0 + timedelta(hours=1), amount=50000)], [], policy) == "NOT_FOUND"
    credit = {"original_transaction_id": "T2", "amount_minor": 49900}
    assert duplicate_check(disputed, [disputed, charge("T2", t0 + timedelta(hours=2))], [credit], policy) == "CREDIT_ALREADY_POSTED"


def test_credit_check_and_fast_path(policy):
    disputed = {"transaction_id": "T1"}
    assert credit_check(disputed, [{"original_transaction_id": "T1", "amount_minor": 1000}], 1000) == "CREDIT_ALREADY_POSTED"
    assert credit_check(disputed, [{"original_transaction_id": "T1", "amount_minor": 400}], 1000) == "PARTIAL_CREDIT_FOUND"
    assert credit_check(disputed, [], 1000) == "NO_CREDIT_FOUND"
    assert fast_path("P08", "CONFIRMED", 1000, "INR", policy) == ("CARDMEMBER_REFUND", "FP1_DUPLICATE_CONFIRMED")
    assert fast_path("P08", "CONFIRMED", 6_000_000, "INR", policy) is None
    assert fast_path("P08", "INCONCLUSIVE", 1000, "INR", policy) is None
    assert fast_path("C02", "NO_CREDIT_FOUND", 1000, "INR", policy) is None


def test_carrier_mapping():
    assert carrier_result_to_evidence("shipment_tracking", "DELIVERED", "110001", "110001") == ("carrier_delivery_confirmation", "system_verified")
    assert carrier_result_to_evidence("shipment_tracking", "DELIVERED", "560001", "110001") == ("carrier_wrong_address", "system_verified")
    assert carrier_result_to_evidence("shipment_tracking", "IN_TRANSIT", None, "110001") == ("carrier_not_delivered", "system_verified")
    assert carrier_result_to_evidence("shipment_tracking", "NOT_FOUND", None, "110001") == ("carrier_delivery_confirmation", "self_attested")
    assert carrier_result_to_evidence("return_tracking", "DELIVERED", "400001", "400001") == ("return_shipment", "system_verified")
    assert carrier_result_to_evidence("return_tracking", "IN_TRANSIT", None, "400001") == ("return_shipment", "self_attested")


# ---------------- redaction ----------------
REDACTION_VECTORS = [
    ("my card 3714 496353 98431", "my card [REDACTED_CARD]"),
    ("my card 3714-496353-98431", "my card [REDACTED_CARD]"),
    ("my card 371449635398431", "my card [REDACTED_CARD]"),
    ("card 2221000000000009", "card [REDACTED_CARD]"),
    ("Order 4000123412341235 not delivered", "Order 4000123412341235 not delivered"),
    ("aadhaar 2363 0000 0000 x", None),
    ("ssn 123-45-6789", "ssn [REDACTED_SSN]"),
    ("pan ABCDE1234F", "pan [REDACTED_PAN_CARD]"),
    ("call +91 98765 43210", "call [REDACTED_PHONE]"),
    ("call 09876543210", "call [REDACTED_PHONE]"),
    ("mail a.b@example.co.in", "mail [REDACTED_EMAIL]"),
    ("pay me at rahul.k@okhdfcbank", "pay me at [REDACTED_UPI]"),
    ("tracking AWB 1234567890 via BlueDart", "tracking AWB 1234567890 via BlueDart"),
]


@pytest.mark.parametrize("raw,expected", [v for v in REDACTION_VECTORS if v[1] is not None])
def test_redaction_vectors(raw, expected):
    assert redact(raw) == expected


def test_verhoeff_and_luhn():
    assert verhoeff_is_valid("2363")
    assert not verhoeff_is_valid("2364")
    assert luhn_is_valid("371449635398431")
    assert not luhn_is_valid("4000123412341235")


def make_valid_aadhaar(prefix11):
    for last in range(10):
        candidate = prefix11 + str(last)
        if verhoeff_is_valid(candidate):
            return candidate
    raise AssertionError


def test_aadhaar_redacted_only_when_checksum_valid():
    valid = make_valid_aadhaar("23456789012")
    spaced = f"{valid[0:4]} {valid[4:8]} {valid[8:12]}"
    assert redact(f"id {spaced}") == "id [REDACTED_AADHAAR]"
    invalid = valid[:-1] + str((int(valid[-1]) + 1) % 10)
    assert "[REDACTED_AADHAAR]" not in redact(f"id {invalid}")


# ---------------- ledger ----------------
def build_chain(count, key):
    rows = []
    prev = L.GENESIS_HASH
    for seq in range(1, count + 1):
        created = L.format_timestamp(datetime(2026, 9, 1, 12, 0, seq, tzinfo=timezone.utc))
        payload = L.canonical_json({"state_to": "DECIDED", "verdict": "MERCHANT_UPHELD", "margin_bp": 5600})
        h = L.entry_hash(seq, prev, f"d-{seq}", "DECISION_RECORDED", "SYSTEM", created, payload)
        rows.append({"seq": seq, "prev_hash": prev, "dispute_id": f"d-{seq}", "event_type": "DECISION_RECORDED",
                     "actor": "SYSTEM", "created_at_text": created, "payload_canonical": payload,
                     "entry_hash": h, "key_id": "k1", "signature": L.sign_hash(key, h)})
        prev = h
    return rows


def test_ledger_verifies_and_detects_tampering_and_truncation():
    key = Ed25519PrivateKey.generate()
    keys = {"k1": key.public_key()}
    rows = build_chain(10, key)
    assert L.verify_chain(rows, keys)[0]
    tampered = [dict(r) for r in rows]
    tampered[4]["payload_canonical"] = tampered[4]["payload_canonical"].replace("MERCHANT_UPHELD", "CARDMEMBER_REFUND")
    assert L.verify_chain(tampered, keys) == (False, "hash mismatch at seq 5")
    cp_created = L.format_timestamp(datetime(2026, 9, 2, tzinfo=timezone.utc))
    cp = {"seq": 10, "entry_hash": rows[9]["entry_hash"], "created_at": cp_created, "key_id": "k1"}
    cp["signature"] = L.sign_hash(key, L.checkpoint_hash(10, rows[9]["entry_hash"], cp_created))
    assert L.verify_chain(rows, keys, cp)[0]
    ok, message = L.verify_chain(rows[:8], keys, cp)
    assert not ok and "truncated" in message
    forged_key = Ed25519PrivateKey.generate()
    forged = build_chain(10, forged_key)
    assert L.verify_chain(forged, keys) == (False, "bad signature at seq 1")


def test_ledger_detects_broken_links_gaps_and_forged_checkpoints():
    # T-UNIT-LED-01 (links, gaps) and T-UNIT-LED-03 (checkpoint signatures)
    key = Ed25519PrivateKey.generate()
    keys = {"k1": key.public_key()}
    rows = build_chain(6, key)
    # a row whose prev_hash does not point at the previous entry (an inserted or re-ordered event)
    relinked = [dict(r) for r in rows]
    relinked[3]["prev_hash"] = "f" * 64
    assert L.verify_chain(relinked, keys) == (False, "broken link at seq 4")
    # a deleted event in the middle of the chain
    assert L.verify_chain(rows[:2] + rows[3:], keys) == (False, "sequence gap: expected 3, found 4")
    # a checkpoint signed by someone else's key, or with an edited hash, is rejected
    created = L.format_timestamp(datetime(2026, 9, 2, tzinfo=timezone.utc))
    forged_cp = {"seq": 6, "entry_hash": rows[5]["entry_hash"], "created_at": created, "key_id": "k1",
                 "signature": L.sign_hash(Ed25519PrivateKey.generate(), L.checkpoint_hash(6, rows[5]["entry_hash"], created))}
    assert L.verify_chain(rows, keys, forged_cp) == (False, "checkpoint signature invalid")
    edited_cp = dict(forged_cp, signature=L.sign_hash(key, L.checkpoint_hash(6, rows[5]["entry_hash"], created)),
                     entry_hash="0" * 64)
    assert L.verify_chain(rows, keys, edited_cp) == (False, "checkpoint signature invalid")
    unknown_key_cp = dict(forged_cp, key_id="nobody")
    assert L.verify_chain(rows, keys, unknown_key_cp) == (False, "checkpoint signature invalid")


def test_canonical_json_rejects_floats_and_is_stable():
    with pytest.raises(ValueError):
        L.canonical_json({"score": 0.5})
    assert L.canonical_json({"b": 1, "a": [True, None, "x"]}) == '{"a":[true,null,"x"],"b":1}'
