"""Startup helpers shared by the api and the worker. Any problem here stops the process (fail fast)."""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.domain.policy import load_policy
from app.gateways.carrier import MockCarrier
from app.gateways.core import MockCore
from app.services.common import Ctx


def load_signing_key(path):
    with open(path, "rb") as handle:
        key = serialization.load_pem_private_key(handle.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("LEDGER_SIGNING_KEY_PATH must contain an Ed25519 private key")
    return key


def build_engine_and_sessions(settings):
    engine = create_async_engine(settings.database_url, pool_size=10, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def build_ctx(settings):
    policy = load_policy(settings.policy_path)
    signing_key = load_signing_key(settings.ledger_signing_key_path)
    return Ctx(policy=policy, signing_key=signing_key, key_id=settings.ledger_key_id,
               core=MockCore(), carrier=MockCarrier.from_file(settings.carrier_fixtures_path))


async def register_public_key_and_load_all(sessions, ctx):
    """Make sure our public key is in ledger_keys; return {key_id: public_key} for verification."""
    public_pem = ctx.signing_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    async with sessions() as session, session.begin():
        await session.execute(text(
            "INSERT INTO ledger_keys (key_id, algorithm, public_key_pem) VALUES (:k, 'Ed25519', :pem) "
            "ON CONFLICT (key_id) DO NOTHING"), {"k": ctx.key_id, "pem": public_pem})
        rows = (await session.execute(text("SELECT key_id, public_key_pem FROM ledger_keys"))).all()
    public_keys = {}
    for row in rows:
        public_keys[row.key_id] = serialization.load_pem_public_key(row.public_key_pem.encode())
    # a key id that is registered with a DIFFERENT public key means a configuration mistake
    if public_keys[ctx.key_id].public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode() != public_pem:
        raise RuntimeError("LEDGER_KEY_ID is already registered with a different key")
    return public_keys
