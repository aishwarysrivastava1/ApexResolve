# Startup guards and the route inventory (no database needed):
#   T-SEC-13 no endpoint can trigger decisions, timers or settlement
#   T-SEC-14 the dev IdP never runs in production, and the api refuses the dev issuer in production
#   T-SEC-15 missing or malformed configuration and keys stop the process at startup
#   T-SEC-20 CORS only for configured origins; API docs disabled in production
import os
import tempfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from app.bootstrap import load_signing_key
from app.main import create_app
from app.settings import ApiSettings, CommonSettings
from devidp.main import create_devidp

BASE = {"app_env": "test", "database_url": "postgresql+asyncpg://apex_app@localhost/apex", "policy_path": "p.yaml",
        "ledger_signing_key_path": "k.pem", "ledger_key_id": "ledger-test", "oidc_issuer": "https://idp.example",
        "oidc_jwks_url": "https://idp.example/jwks.json"}

# every endpoint the API offers (method, path); anything new must be added here and to APX-07
EXPECTED_ROUTES = {
    ("GET", "/healthz"), ("GET", "/readyz"),
    ("GET", "/api/v1/me/transactions"), ("GET", "/api/v1/disputes"), ("POST", "/api/v1/disputes"),
    ("GET", "/api/v1/disputes/{dispute_id}"), ("GET", "/api/v1/disputes/{dispute_id}/timeline"),
    ("POST", "/api/v1/disputes/{dispute_id}/evidence"), ("GET", "/api/v1/disputes/{dispute_id}/files/{file_id}"),
    ("POST", "/api/v1/disputes/{dispute_id}/contest"), ("POST", "/api/v1/disputes/{dispute_id}/accept"),
    ("POST", "/api/v1/disputes/{dispute_id}/withdraw"), ("POST", "/api/v1/disputes/{dispute_id}/rebuttal-done"),
    ("POST", "/api/v1/disputes/{dispute_id}/offer-response"), ("POST", "/api/v1/disputes/{dispute_id}/appeal"),
    ("POST", "/api/v1/disputes/{dispute_id}/review-decision"), ("GET", "/api/v1/review/queue"),
    ("GET", "/api/v1/audit/events"), ("POST", "/api/v1/audit/verify"), ("POST", "/api/v1/audit/checkpoints"),
    ("GET", "/api/v1/audit/checkpoints/latest"), ("GET", "/api/v1/audit/public-keys"),
    ("GET", "/api/v1/metrics/summary"), ("GET", "/api/v1/notifications"),
    ("POST", "/api/v1/notifications/{notification_id}/read"), ("GET", "/api/v1/policy"),
}


def api_routes(app):
    # the OpenAPI document lists every route of every included router
    routes = set()
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            routes.add((method.upper(), path))
    return routes


def test_t_sec_13_route_inventory_has_no_engine_triggers():
    routes = api_routes(create_app(ApiSettings(**BASE)))
    assert routes == EXPECTED_ROUTES
    for _method, path in routes:
        for word in ("evaluate", "decide", "finalize", "settle", "transfer", "job", "worker"):
            assert word not in path, path


def test_t_sec_14_dev_idp_refuses_production_and_weak_passwords():
    with pytest.raises(RuntimeError):
        create_devidp("prod", "apexresolve-devidp", "apexresolve-api", {}, "a-long-enough-password")
    with pytest.raises(RuntimeError):
        create_devidp("demo", "apexresolve-devidp", "apexresolve-api", {}, "short")
    with pytest.raises(ValidationError):
        ApiSettings(**(BASE | {"app_env": "prod", "oidc_issuer": "apexresolve-devidp"}))


def test_t_sec_15_configuration_fails_fast():
    for broken in ({"app_env": "staging"}, {"database_url": "sqlite:///apex.db"}, {"ledger_key_id": ""}):
        with pytest.raises(ValidationError):
            CommonSettings(**(BASE | broken))
    incomplete = dict(BASE)
    del incomplete["ledger_signing_key_path"]
    with pytest.raises(ValidationError):
        CommonSettings(**incomplete)
    # a signing key that is not Ed25519 is refused
    path = os.path.join(tempfile.mkdtemp(), "wrong.pem")
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(path, "wb") as handle:
        handle.write(rsa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    with pytest.raises(RuntimeError):
        load_signing_key(path)


def test_t_sec_20_cors_and_docs():
    plain = create_app(ApiSettings(**BASE))
    assert plain.docs_url == "/docs"
    assert not any(m.cls is CORSMiddleware for m in plain.user_middleware)
    production = create_app(ApiSettings(**(BASE | {"app_env": "prod", "cors_origins": "https://app.example"})))
    assert production.docs_url is None and production.openapi_url is None
    cors = [m for m in production.user_middleware if m.cls is CORSMiddleware]
    assert len(cors) == 1 and cors[0].kwargs["allow_origins"] == ["https://app.example"]
