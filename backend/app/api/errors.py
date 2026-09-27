"""RFC 9457 problem responses. Validation errors never echo the submitted value."""
from fastapi import Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.logs import error_location, log
from app.services.common import ServiceError

TITLES = {
    400: "Bad request", 401: "Unauthorized", 403: "Forbidden", 404: "Not found", 409: "Conflict",
    413: "File too large", 415: "Unsupported file type", 422: "Invalid request", 429: "Too many requests",
    500: "Internal error", 503: "Unavailable",
}
DEFAULT_PROBLEM = {400: "bad-request", 401: "unauthorized", 403: "forbidden", 404: "not-found", 409: "illegal-state",
                   422: "validation-error", 429: "rate-limited", 500: "internal-error", 503: "maintenance"}


def problem(request, status, problem_type, detail="", extra=None, retry_after=None):
    body = {"type": f"urn:apexresolve:problem:{problem_type}", "title": TITLES.get(status, "Error"),
            "status": status, "detail": detail, "instance": request.url.path,
            "request_id": getattr(request.state, "request_id", None)}
    if extra:
        body.update(extra)
    headers = None
    if retry_after is not None:
        # seconds the client should wait before trying again (429 responses)
        headers = {"Retry-After": str(retry_after)}
    return JSONResponse(status_code=status, content=body, media_type="application/problem+json", headers=headers)


async def service_error_handler(request: Request, exc: ServiceError):
    return problem(request, exc.status, exc.problem, exc.detail, retry_after=getattr(exc, "retry_after", None))


async def validation_error_handler(request: Request, exc: RequestValidationError):
    # field path and error type only; the input value is deliberately dropped
    errors = []
    for err in exc.errors():
        errors.append({"field": ".".join(str(part) for part in err["loc"]), "type": err["type"]})
    return problem(request, 422, "validation-error", "the request is invalid", {"errors": errors})


async def http_error_handler(request: Request, exc: HTTPException):
    retry_after = 60 if exc.status_code == 429 else None     # the per-minute limit resets within 60 s
    return problem(request, exc.status_code, DEFAULT_PROBLEM.get(exc.status_code, "error"), str(exc.detail),
                   retry_after=retry_after)


async def unexpected_error_handler(request: Request, exc: Exception):
    # details go to the log (by request_id), never to the client; the message is not logged (it may hold data)
    log("error", request_id=getattr(request.state, "request_id", None), error_type=type(exc).__name__,
        where=error_location(exc))
    return problem(request, 500, "internal-error", "unexpected error")


def install(app):
    app.add_exception_handler(ServiceError, service_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
