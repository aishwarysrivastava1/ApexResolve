"""Reference auth dependency for FastAPI: async JWKS cache + strict JWT validation.

Copy into backend/app/auth.py. Settings are passed in explicitly so tests can build their own.
"""
import time
import jwt
from fastapi import HTTPException

ALLOWED_ALGORITHMS = ["RS256", "ES256"]
ROLES = ["cardmember", "merchant", "reviewer", "auditor"]


class JwksCache:
    """Downloads the IdP's public keys once and refreshes them when an unknown key id appears."""

    def __init__(self, jwks_url, http_client, min_refresh_seconds=60):
        self.jwks_url = jwks_url
        self.http_client = http_client
        self.min_refresh_seconds = min_refresh_seconds
        self.keys_by_kid = {}
        self.last_refresh = 0.0

    async def refresh(self):
        # fetch the key set with a short timeout; never block the event loop
        response = await self.http_client.get(self.jwks_url, timeout=5.0)
        response.raise_for_status()
        new_keys = {}
        for jwk in response.json()["keys"]:
            new_keys[jwk["kid"]] = jwt.PyJWK(jwk).key
        self.keys_by_kid = new_keys
        self.last_refresh = time.monotonic()

    async def get_key(self, kid):
        if kid in self.keys_by_kid:
            return self.keys_by_kid[kid]
        # unknown kid: refresh, but not more often than min_refresh_seconds (stops abuse)
        if time.monotonic() - self.last_refresh >= self.min_refresh_seconds or not self.keys_by_kid:
            await self.refresh()
        return self.keys_by_kid.get(kid)


async def validate_token(token, jwks_cache, issuer, audience):
    """Return the claims dict, or raise HTTPException(401/403). Never raises anything else."""
    try:
        header = jwt.get_unverified_header(token)
        # refuse anything outside the allow-list BEFORE looking at keys (blocks alg=none / HS256 tricks)
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise HTTPException(status_code=401, detail="invalid token")
        key = await jwks_cache.get_key(header.get("kid"))
        if key is None:
            raise HTTPException(status_code=401, detail="invalid token")
        claims = jwt.decode(token, key, algorithms=ALLOWED_ALGORITHMS, audience=audience, issuer=issuer,
                            leeway=30, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except HTTPException:
        raise
    except Exception:
        # expired, bad signature, garbage, non-ASCII... all become 401, never 500
        raise HTTPException(status_code=401, detail="invalid token") from None
    role = claims.get("role")
    if role not in ROLES:
        raise HTTPException(status_code=403, detail="unknown role")
    if role == "merchant" and not claims.get("merchant_id"):
        raise HTTPException(status_code=403, detail="merchant token without merchant_id")
    return claims
