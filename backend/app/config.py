from functools import lru_cache

from pydantic import Field
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
    seed_default_sites: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
