# T-PERF-03 (NFR-PERF-04): verifying 10 000 ledger events (hash chain + every Ed25519 signature) takes < 10 s.
import time
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.domain import ledger_math as L

EVENTS = 10_000


def test_verifying_ten_thousand_events_is_fast():
    key = Ed25519PrivateKey.generate()
    start_time = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = []
    previous = L.GENESIS_HASH
    for seq in range(1, EVENTS + 1):
        created = L.format_timestamp(start_time + timedelta(seconds=seq))
        payload = L.canonical_json({"from": "AWAITING_MERCHANT", "to": "AWAITING_CARDMEMBER_REBUTTAL"})
        entry = L.entry_hash(seq, previous, f"dispute-{seq % 500}", "STATE_CHANGED", "SYSTEM", created, payload)
        rows.append({"seq": seq, "prev_hash": previous, "dispute_id": f"dispute-{seq % 500}",
                     "event_type": "STATE_CHANGED", "actor": "SYSTEM", "created_at_text": created,
                     "payload_canonical": payload, "entry_hash": entry, "key_id": "k1",
                     "signature": L.sign_hash(key, entry)})
        previous = entry
    started = time.monotonic()
    ok, message = L.verify_chain(rows, {"k1": key.public_key()})
    elapsed = time.monotonic() - started
    assert ok, message
    assert elapsed < 10, f"verification took {elapsed:.1f} s"
