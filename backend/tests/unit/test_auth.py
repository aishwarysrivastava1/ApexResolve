import asyncio, base64, hashlib, hmac, time, json
import httpx, jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from app.auth import JwksCache, validate_token

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISS, AUD = "http://localhost:9000", "apexresolve-api"


def jwks_for(private_key, kid):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update(kid=kid, alg="RS256", use="sig")
    return {"keys": [jwk]}


calls = {"n": 0}


def handler(request):
    calls["n"] += 1
    return httpx.Response(200, json=jwks_for(KEY, "k1"))


def token(claims=None, key=KEY, kid="k1", alg="RS256"):
    now = int(time.time())
    base = {"iss": ISS, "aud": AUD, "sub": "cm-asha", "role": "cardmember", "iat": now, "exp": now + 300}
    base.update(claims or {})
    return jwt.encode(base, key, algorithm=alg, headers={"kid": kid})


async def run(tok):
    cache = JwksCache("http://devidp/jwks.json", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        claims = await validate_token(tok, cache, ISS, AUD)
        return 200, claims["sub"]
    except HTTPException as e:
        return e.status_code, e.detail


def hs256_with_public_key(claims):
    # the classic algorithm-confusion attack: an HMAC "signature" keyed with the server's PUBLIC key text
    public_pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    header = {"alg": "HS256", "typ": "JWT", "kid": "k1"}
    signing_input = b64url(json.dumps(header).encode()) + "." + b64url(json.dumps(claims).encode())
    signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + b64url(signature)


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_matrix():
    # T-SEC-01…09: every forged, expired, misdirected or malformed token is refused, never with a 500
    now = int(time.time())
    auditor_claims = {"iss": ISS, "aud": AUD, "sub": "x", "role": "auditor", "iat": now, "exp": now + 60}
    cases = {
        "hs256 signed with the public key (alg confusion)": (hs256_with_public_key(auditor_claims), 401),
        "hs256 with a guessed secret": (jwt.encode(auditor_claims, "secret", algorithm="HS256", headers={"kid": "k1"}), 401),
        "valid": (token(), 200),
        "forged prefix": ("merch_token_M_ACME", 401),
        "attacker key": (token(key=OTHER), 401),
        "expired": (token({"exp": now - 3600, "iat": now - 7200}), 401),
        "wrong audience": (token({"aud": "other"}), 401),
        "wrong issuer": (token({"iss": "http://evil"}), 401),
        "alg none": (jwt.encode({"iss": ISS, "aud": AUD, "sub": "x", "role": "cardmember", "iat": now, "exp": now + 60}, None, algorithm="none"), 401),
        "unknown role": (token({"role": "system"}), 403),
        "merchant without merchant_id": (token({"role": "merchant"}), 403),
        "garbage": ("café.not.a.jwt", 401),
    }
    for name, (tok, expected) in cases.items():
        status, _ = asyncio.run(run(tok))
        assert status == expected, (name, status)
