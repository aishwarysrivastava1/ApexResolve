"""Principal extraction, role checks, security headers, request ids and a simple per-subject rate limit."""
import re
import time
import uuid
from collections import defaultdict, deque
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.auth import validate_token
from app.api.errors import problem
from app.logs import log, subject_hash

bearer = HTTPBearer(auto_error=False)
_hits = defaultdict(deque)             # single api instance assumption (documented limitation)
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


async def get_principal(request: Request, creds: HTTPAuthorizationCredentials = Depends(bearer)):
    if creds is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    settings = request.app.state.settings
    claims = await validate_token(creds.credentials, request.app.state.jwks, settings.oidc_issuer,
                                  settings.oidc_audience)
    principal = {"sub": claims["sub"], "role": claims["role"], "merchant_id": claims.get("merchant_id")}
    request.state.sub_hash = subject_hash(principal["sub"])      # for the access log only
    # sliding one-minute window per subject
    window = _hits[principal["sub"]]
    now = time.monotonic()
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= settings.rate_limit_per_minute:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    window.append(now)
    return principal


def check_role(principal, allowed):
    if principal["role"] not in allowed:
        raise HTTPException(status_code=403, detail="this role cannot use this endpoint")
    return principal


# one small dependency per audience (flat functions, no factories)
async def cardmember_only(principal=Depends(get_principal)):
    return check_role(principal, ["cardmember"])


async def merchant_only(principal=Depends(get_principal)):
    return check_role(principal, ["merchant"])


async def party_only(principal=Depends(get_principal)):
    return check_role(principal, ["cardmember", "merchant"])


async def reviewer_only(principal=Depends(get_principal)):
    return check_role(principal, ["reviewer"])


async def auditor_only(principal=Depends(get_principal)):
    return check_role(principal, ["auditor"])


async def staff_only(principal=Depends(get_principal)):
    return check_role(principal, ["reviewer", "auditor"])


async def any_role(principal=Depends(get_principal)):
    return principal


async def security_headers_middleware(request: Request, call_next):
    started = time.monotonic()
    # accept a caller's request id only if it is short and plain; otherwise make our own
    incoming = request.headers.get("x-request-id", "")
    request.state.request_id = incoming if REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
    if request.app.state.settings.maintenance_mode and request.method not in ("GET", "HEAD", "OPTIONS"):
        response = problem(request, 503, "maintenance", "writes are paused for maintenance")
    else:
        response = await call_next(request)
    # one access-log line: the route template (never the raw path or query), status and duration
    route = request.scope.get("route")
    log("request", request_id=request.state.request_id, method=request.method,
        route=route.path if route is not None else "unmatched", status=response.status_code,
        duration_ms=round((time.monotonic() - started) * 1000, 1), sub=getattr(request.state, "sub_hash", None))
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    response.headers["X-Request-Id"] = request.state.request_id
    return response
