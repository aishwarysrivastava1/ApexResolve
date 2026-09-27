"""The dispute lifecycle as an explicit transition table. Pure; no database."""

STATES = [
    "REJECTED_INELIGIBLE",
    "AWAITING_MERCHANT",
    "AWAITING_CARDMEMBER_REBUTTAL",
    "READY_FOR_DECISION",
    "SETTLEMENT_OFFERED",
    "HUMAN_REVIEW",
    "DECIDED",
    "SETTLEMENT_PENDING",
    "CLOSED",
]

# states a new dispute can be created in (filing is creation, not a transition)
INITIAL_STATES = ["REJECTED_INELIGIBLE", "AWAITING_MERCHANT", "DECIDED"]

TERMINAL_STATES = ["REJECTED_INELIGIBLE", "CLOSED"]

# (current_state, event) -> next_state. Any pair not listed is illegal.
TRANSITIONS = {
    ("AWAITING_MERCHANT", "MERCHANT_CONTESTED"): "AWAITING_CARDMEMBER_REBUTTAL",
    ("AWAITING_MERCHANT", "MERCHANT_ACCEPTED"): "DECIDED",
    ("AWAITING_MERCHANT", "MERCHANT_DEADLINE_PASSED"): "DECIDED",
    ("AWAITING_MERCHANT", "CARDMEMBER_WITHDREW"): "DECIDED",
    ("AWAITING_CARDMEMBER_REBUTTAL", "CARDMEMBER_REBUTTAL_DONE"): "READY_FOR_DECISION",
    ("AWAITING_CARDMEMBER_REBUTTAL", "REBUTTAL_DEADLINE_PASSED"): "READY_FOR_DECISION",
    ("AWAITING_CARDMEMBER_REBUTTAL", "CARDMEMBER_WITHDREW"): "DECIDED",
    ("READY_FOR_DECISION", "AUTO_DECIDED"): "DECIDED",
    ("READY_FOR_DECISION", "OFFER_MADE"): "SETTLEMENT_OFFERED",
    ("READY_FOR_DECISION", "SENT_TO_REVIEW"): "HUMAN_REVIEW",
    ("SETTLEMENT_OFFERED", "OFFER_ACCEPTED_BY_BOTH"): "DECIDED",
    ("SETTLEMENT_OFFERED", "OFFER_DECLINED"): "HUMAN_REVIEW",
    ("SETTLEMENT_OFFERED", "OFFER_DEADLINE_PASSED"): "HUMAN_REVIEW",
    ("HUMAN_REVIEW", "REVIEWER_DECIDED"): "DECIDED",
    ("DECIDED", "APPEAL_FILED"): "HUMAN_REVIEW",
    ("DECIDED", "FINALIZED_WITH_TRANSFER"): "SETTLEMENT_PENDING",
    ("DECIDED", "FINALIZED_NO_TRANSFER"): "CLOSED",
    ("SETTLEMENT_PENDING", "TRANSFER_CONFIRMED"): "CLOSED",
}

EVENTS = sorted({event for (_, event) in TRANSITIONS})


class IllegalTransition(Exception):
    pass


def next_state(current_state, event):
    # look the pair up; anything not in the table is rejected
    key = (current_state, event)
    if key not in TRANSITIONS:
        raise IllegalTransition(f"{event} is not allowed in state {current_state}")
    return TRANSITIONS[key]
