"""Redact personal data from free text before it is stored. Pure; no I/O.

Order matters: card numbers first (Luhn-checked), then Aadhaar (Verhoeff-checked),
then SSN, PAN card, phone, email and UPI handles.
Structured fields (tracking numbers, order references) are validated, not redacted.
"""
import re

CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
AADHAAR_CANDIDATE = re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)")
SSN = re.compile(r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)")
PAN_CARD = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
IN_PHONE = re.compile(r"(?<!\d)(?:\+91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UPI = re.compile(r"\b[A-Za-z0-9._-]{2,256}@[A-Za-z]{2,64}\b")

# Verhoeff tables (used by Aadhaar check digits)
VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def luhn_is_valid(digits):
    # double every second digit from the right; valid numbers sum to a multiple of 10
    total = 0
    position = 0
    for ch in reversed(digits):
        value = int(ch)
        if position % 2 == 1:
            value = value * 2
            if value > 9:
                value = value - 9
        total = total + value
        position = position + 1
    return total % 10 == 0


def verhoeff_is_valid(digits):
    # the Verhoeff checksum of a valid number is 0
    check = 0
    position = 0
    for ch in reversed(digits):
        check = VERHOEFF_D[check][VERHOEFF_P[position % 8][int(ch)]]
        position = position + 1
    return check == 0


def replace_checked(text, pattern, min_len, max_len, checker, label):
    # replace only the matches whose bare digits have the right length and pass the checksum
    pieces = []
    last_end = 0
    for match in pattern.finditer(text):
        bare = match.group(0).replace(" ", "").replace("-", "")
        if min_len <= len(bare) <= max_len and checker(bare):
            pieces.append(text[last_end:match.start()])
            pieces.append(label)
            last_end = match.end()
    pieces.append(text[last_end:])
    return "".join(pieces)


def redact(text):
    if not text:
        return text
    text = replace_checked(text, CARD_CANDIDATE, 13, 19, luhn_is_valid, "[REDACTED_CARD]")
    text = replace_checked(text, AADHAAR_CANDIDATE, 12, 12, verhoeff_is_valid, "[REDACTED_AADHAAR]")
    text = SSN.sub("[REDACTED_SSN]", text)
    text = PAN_CARD.sub("[REDACTED_PAN_CARD]", text)
    text = IN_PHONE.sub("[REDACTED_PHONE]", text)
    text = EMAIL.sub("[REDACTED_EMAIL]", text)
    text = UPI.sub("[REDACTED_UPI]", text)
    return text
