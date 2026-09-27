"""Development-only identity provider. Issues RS256 JWTs for seeded demo users and publishes its JWKS.

Refuses to run when APP_ENV=prod. The RSA key lives in memory only (a restart invalidates all tokens).
"""
import hmac
import json
import os
import time
import uuid
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

router = APIRouter()


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str


@router.get("/jwks.json")
async def jwks(request: Request):
    return {"keys": [request.app.state.public_jwk]}


@router.post("/token")
async def token(request: Request, body: TokenRequest):
    idp = request.app.state
    user = idp.users.get(body.username)
    password_ok = hmac.compare_digest(body.password.encode("utf-8"), idp.demo_password.encode("utf-8"))
    if user is None or not password_ok:
        raise HTTPException(status_code=401, detail="invalid credentials")
    now = int(time.time())
    claims = {"iss": idp.issuer, "aud": idp.audience, "sub": body.username, "role": user["role"],
              "name": user["name"], "iat": now, "exp": now + 3600}
    if user.get("merchant_id"):
        claims["merchant_id"] = user["merchant_id"]
    signed = jwt.encode(claims, idp.private_key, algorithm="RS256", headers={"kid": idp.kid})
    return {"access_token": signed, "token_type": "Bearer", "expires_in": 3600}


def create_devidp(app_env, issuer, audience, users, demo_password):
    # hard guards: never in production, never without a real password
    if app_env not in ("dev", "test", "demo"):
        raise RuntimeError("the dev IdP must never run with APP_ENV=prod")
    if not demo_password or len(demo_password) < 12:
        raise RuntimeError("DEVIDP_DEMO_PASSWORD must be set (at least 12 characters)")
    app = FastAPI(title="ApexResolve dev IdP (NOT FOR PRODUCTION)", docs_url=None, openapi_url=None)
    app.state.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    app.state.kid = uuid.uuid4().hex
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(app.state.private_key.public_key()))
    public_jwk.update({"kid": app.state.kid, "alg": "RS256", "use": "sig"})
    app.state.public_jwk = public_jwk
    app.state.issuer = issuer
    app.state.audience = audience
    app.state.users = users
    app.state.demo_password = demo_password
    app.include_router(router)
    return app


def app_from_env():
    with open(os.environ["DEVIDP_USERS_PATH"], "r", encoding="utf-8") as handle:
        users = json.load(handle)
    return create_devidp(os.environ["APP_ENV"], os.environ["DEVIDP_ISSUER"],
                         os.environ.get("DEVIDP_AUDIENCE", "apexresolve-api"), users,
                         os.environ.get("DEVIDP_DEMO_PASSWORD", ""))
