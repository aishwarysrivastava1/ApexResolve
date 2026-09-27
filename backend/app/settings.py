"""All configuration comes from environment variables. Missing or weak values stop the process at startup."""
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_ISSUER = "apexresolve-devidp"


class CommonSettings(BaseSettings):
    """Settings shared by the api and the worker."""
    # hide_input_in_errors: a startup error names the bad variable but never prints its value (it may be a password)
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore", hide_input_in_errors=True)

    app_env: str = Field(pattern="^(dev|test|demo|prod)$")
    database_url: str = Field(min_length=10)              # apex_app role, postgresql+asyncpg://...
    policy_path: str
    ledger_signing_key_path: str
    ledger_key_id: str = Field(min_length=3)
    carrier_fixtures_path: str = "fixtures/carrier.json"

    @field_validator("database_url")
    @classmethod
    def must_be_postgres(cls, value):
        # there is no SQLite fallback anywhere
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must be a postgresql+asyncpg:// URL")
        return value


class ApiSettings(CommonSettings):
    """Extra settings only the api needs."""
    oidc_issuer: str
    oidc_audience: str = "apexresolve-api"
    oidc_jwks_url: str
    cors_origins: str = ""                                  # comma-separated exact origins
    rate_limit_per_minute: int = Field(default=120, ge=1)      # one SPA screen makes up to ~5 requests
    maintenance_mode: bool = False

    @model_validator(mode="after")
    def no_dev_idp_in_prod(self):
        if self.app_env == "prod" and self.oidc_issuer == DEV_ISSUER:
            raise ValueError("the dev IdP issuer is not allowed when APP_ENV=prod")
        return self

    def cors_list(self):
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
