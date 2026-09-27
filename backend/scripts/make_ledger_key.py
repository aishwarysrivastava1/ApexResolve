"""Create an Ed25519 ledger signing key file (PEM, mode 0600). Never commit the output.

Usage: python scripts/make_ledger_key.py secrets/ledger_signing_key.pem
"""
import os
import sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    path = sys.argv[1]
    if os.path.exists(path):
        sys.exit(f"{path} already exists; refusing to overwrite a signing key")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
