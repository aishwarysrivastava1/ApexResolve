"""Display helpers used by explanations and the API. Pure."""
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.decision import to_bp


def format_inr(amount_minor):
    # 12345678 -> "₹1,23,456.78" (Indian digit grouping)
    sign = "-" if amount_minor < 0 else ""
    amount_minor = abs(amount_minor)
    rupees = amount_minor // 100
    paise = amount_minor % 100
    digits = str(rupees)
    if len(digits) > 3:
        head = digits[:-3]
        tail = digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    return f"{sign}₹{digits}.{paise:02d}"


def format_money(amount_minor, currency):
    if currency == "INR":
        return format_inr(amount_minor)
    return f"{currency} {Decimal(amount_minor) / 100:.2f}"


def pct_text(value):
    # exact fraction -> percentage text with at most 2 decimals: 0.9 -> "90", 0.045 -> "4.5", 0.85675 -> "85.68"
    percent = Decimal(to_bp(value)) / 100
    text = f"{percent:.2f}".rstrip("0").rstrip(".")
    return text


def local_date_text(moment, tz_name):
    return moment.astimezone(ZoneInfo(tz_name)).strftime("%d %b %Y")


def humanize(evidence_type):
    return evidence_type.replace("_", " ")


SOURCE_LABEL = {"system_verified": "verified by system", "document": "document", "self_attested": "statement"}


def items_text(evidence, side, reason_code, policy):
    # "signed proof of delivery (document), merchant statement (statement)" — only relevant items of one side
    catalogue = policy["evidence_catalogue"][reason_code]
    parts = []
    for item in evidence:
        entry = catalogue.get(item["evidence_type"])
        if entry is None or entry["side"] != side:
            continue
        label = f"{humanize(item['evidence_type'])} ({SOURCE_LABEL[item['source']]})"
        if label not in parts:
            parts.append(label)
    if not parts:
        return "none"
    return ", ".join(parts)
