"""Pure ledger maths: canonical encoding, entry hashes, signatures and chain verification.

Storage (the audit_events table and the head-row lock) lives in the service layer.
"""
import hashlib
import json
from datetime import timezone

DOMAIN_ENTRY = "apexresolve-ledger-v2"
DOMAIN_CHECKPOINT = "apexresolve-checkpoint-v2"
GENESIS_HASH = "0" * 64


def check_canonical_value(value, path="payload"):
    # payloads may only contain strings, integers, booleans, null, lists and dicts with ASCII keys
    if isinstance(value, float):
        raise ValueError(f"{path}: floats are not allowed in ledger payloads (use integers or strings)")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isascii():
                raise ValueError(f"{path}: keys must be ASCII strings")
            check_canonical_value(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_canonical_value(item, f"{path}[{index}]")
    elif not (value is None or isinstance(value, (str, int, bool))):
        raise ValueError(f"{path}: unsupported type {type(value).__name__}")


def canonical_json(value):
    # one byte string per logical value: sorted keys, no spaces, UTF-8
    check_canonical_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def format_timestamp(moment):
    # always UTC, always 6 fractional digits, always a trailing Z
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def entry_hash(seq, prev_hash, dispute_id, event_type, actor, created_at_text, payload_canonical):
    envelope = canonical_json([DOMAIN_ENTRY, seq, prev_hash, dispute_id, event_type, actor,
                               created_at_text, payload_canonical])
    return hashlib.sha256(envelope.encode("utf-8")).hexdigest()


def checkpoint_hash(seq, entry_hash_hex, created_at_text):
    envelope = canonical_json([DOMAIN_CHECKPOINT, seq, entry_hash_hex, created_at_text])
    return hashlib.sha256(envelope.encode("utf-8")).hexdigest()


def sign_hash(private_key, hash_hex):
    return private_key.sign(bytes.fromhex(hash_hex)).hex()


def verify_signature(public_key, hash_hex, signature_hex):
    try:
        public_key.verify(bytes.fromhex(signature_hex), bytes.fromhex(hash_hex))
        return True
    except Exception:
        return False


def verify_chain(rows, public_keys, checkpoint=None):
    """rows: audit events ordered by seq (dicts with the stored columns).
    public_keys: {key_id: Ed25519PublicKey}. checkpoint: optional {seq, entry_hash, created_at, key_id, signature}.
    Returns (ok, message)."""
    expected_prev = GENESIS_HASH
    expected_seq = 1
    hashes_by_seq = {}
    for row in rows:
        if row["seq"] != expected_seq:
            return False, f"sequence gap: expected {expected_seq}, found {row['seq']}"
        if row["prev_hash"] != expected_prev:
            return False, f"broken link at seq {row['seq']}"
        recomputed = entry_hash(row["seq"], row["prev_hash"], row["dispute_id"], row["event_type"],
                                row["actor"], row["created_at_text"], row["payload_canonical"])
        if recomputed != row["entry_hash"]:
            return False, f"hash mismatch at seq {row['seq']}"
        key = public_keys.get(row["key_id"])
        if key is None or not verify_signature(key, row["entry_hash"], row["signature"]):
            return False, f"bad signature at seq {row['seq']}"
        hashes_by_seq[row["seq"]] = row["entry_hash"]
        expected_prev = row["entry_hash"]
        expected_seq = expected_seq + 1
    if checkpoint is not None:
        key = public_keys.get(checkpoint["key_id"])
        cp_hash = checkpoint_hash(checkpoint["seq"], checkpoint["entry_hash"], checkpoint["created_at"])
        if key is None or not verify_signature(key, cp_hash, checkpoint["signature"]):
            return False, "checkpoint signature invalid"
        if hashes_by_seq.get(checkpoint["seq"]) != checkpoint["entry_hash"]:
            return False, f"ledger does not contain checkpoint seq {checkpoint['seq']} (truncated or rewritten)"
    return True, f"{len(rows)} entries verified"
