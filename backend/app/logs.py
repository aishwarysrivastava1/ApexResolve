"""Structured logs: one JSON object per line on stdout (NFR-OPS-02).

Never log request or response bodies, statements, notes, file contents, tokens or subjects in clear
(NFR-PRIV-02). A subject appears only as a short SHA-256 pseudonym so one user's requests can be correlated.
"""
import hashlib
import json
import sys
import traceback
from datetime import datetime, timezone


def subject_hash(sub):
    # first 16 hex characters of SHA-256: stable per user, not reversible in practice
    return hashlib.sha256(sub.encode("utf-8")).hexdigest()[:16]


def log(event, **fields):
    record = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "event": event}
    record.update(fields)
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()


def error_location(error):
    # where the error happened (file:line function), without the message, which could contain data
    frames = traceback.extract_tb(error.__traceback__)
    return [f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} {frame.name}" for frame in frames[-5:]]
