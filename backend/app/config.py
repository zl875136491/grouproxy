from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GROUPROXY_", env_file=".env", extra="ignore")

    app_name: str = "grouproxy-backend"
    environment: str = "development"
    mongodb_url: str = "mongodb://127.0.0.1:27017"
    mongodb_database: str = "grouproxy"
    host: str = "0.0.0.0"
    port: int = 8000
    backend_public_url: str = "http://127.0.0.1:8000"
    bundle_hmac_secret: str = Field(min_length=32)
    admin_username: str = "admin"
    admin_password: str = Field(min_length=12)
    management_token: str = Field(min_length=32)
    allow_insecure_agent_http: bool = False
    default_http_port: int = 80
    bundle_ttl_days: int = 30
    subscription_default_interval_sec: int = 21_600
    subscription_max_body_bytes: int = 2_000_000
    subscription_inline_max_bytes: int = 128_000
    subscription_worker_poll_seconds: float = 1.0
    subscription_task_lease_seconds: int = 60
    auth_session_ttl_minutes: int = Field(default=720, ge=15, le=43_200)
    auth_verification_ttl_seconds: int = Field(default=600, ge=60, le=3_600)
    auth_verification_resend_seconds: int = Field(default=60, ge=15, le=600)
    auth_verification_max_attempts: int = Field(default=5, ge=3, le=10)
    gquan_api_base_url: str = "https://one.1oa.com.cn/springboard/api/v1"
    gquan_app_token: SecretStr | None = None
    gquan_delivery_mode: Literal["app", "stub"] = "app"
    gquan_test_code: SecretStr | None = None
    gquan_request_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    seed_default_sites: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
