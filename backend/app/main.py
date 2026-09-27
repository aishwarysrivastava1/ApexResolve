"""API app factory. Run with: uvicorn app.main:app_from_env --factory --host 0.0.0.0 --port 8000"""
from contextlib import asynccontextmanager
import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import errors
from app.api.routes_read import read_router
from app.api.routes_write import router
from app.api.security import security_headers_middleware
from app.auth import JwksCache
from app.bootstrap import build_ctx, build_engine_and_sessions, register_public_key_and_load_all
from app.settings import ApiSettings

health_router = APIRouter()


@asynccontextmanager
async def lifespan(app):
    settings = app.state.settings
    # 1. policy, signing key, gateways, database: any problem stops startup
    app.state.ctx = build_ctx(settings)
    engine, app.state.sessions = build_engine_and_sessions(settings)
    # 2. register our public key so verifiers can check signatures
    app.state.public_keys = await register_public_key_and_load_all(app.state.sessions, app.state.ctx)
    # 3. identity provider public keys (async; never blocks request handling)
    client = app.state.jwks_http_client or httpx.AsyncClient()
    app.state.jwks = JwksCache(settings.oidc_jwks_url, client)
    await app.state.jwks.refresh()
    yield
    await engine.dispose()


@health_router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@health_router.get("/readyz")
async def readyz(request: Request):
    async with request.app.state.sessions() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ready", "policy_version": request.app.state.ctx.policy["policy_version"]}


def create_app(settings, jwks_http_client=None):
    show_docs = settings.app_env != "prod"
    app = FastAPI(title="ApexResolve API", version="2.0.0", lifespan=lifespan,
                  docs_url="/docs" if show_docs else None, openapi_url="/openapi.json" if show_docs else None)
    app.state.settings = settings
    app.state.jwks_http_client = jwks_http_client
    errors.install(app)
    app.middleware("http")(security_headers_middleware)
    if settings.cors_list():
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list(), allow_credentials=False,
                           allow_methods=["GET", "POST"],
                           allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])
    app.include_router(health_router)
    app.include_router(router)
    app.include_router(read_router)
    return app


def app_from_env():
    return create_app(ApiSettings())
