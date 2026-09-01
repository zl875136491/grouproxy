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
    proxy_access_fqdn: str = Field(
        default="proxy.corp.internal",
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$",
    )
    proxy_access_port: int = Field(default=80, ge=1, le=65535)
    probe_auto_enabled: bool = True
    probe_interval_seconds: int = Field(default=300, ge=30, le=86_400)
    probe_target_url: str = "https://www.google.com/ncr"
    probe_max_outbounds: int = Field(default=3, ge=1, le=20)
    deny_spike_window_seconds: int = Field(default=300, ge=60, le=3_600)
    deny_spike_baseline_seconds: int = Field(default=3_600, ge=300, le=86_400)
    deny_spike_min_events: int = Field(default=20, ge=1, le=100_000)
    backup_directory: str = ""
    backup_encryption_key: SecretStr | None = None
    # Backup maintenance is opt-in so a new installation never starts writing
    # archives to an implicit local path before its storage/encryption policy
    # has been configured.
    backup_auto_enabled: bool = False
    backup_auto_interval_seconds: int = Field(default=86_400, ge=300, le=2_592_000)
    backup_rehearsal_interval_seconds: int = Field(default=604_800, ge=3_600, le=7_776_000)
    backup_maintenance_interval_seconds: float = Field(default=60.0, ge=1.0, le=3_600.0)
    backup_retention_daily_days: int = Field(default=7, ge=1, le=365)
    backup_retention_weekly_weeks: int = Field(default=4, ge=0, le=104)
    backup_retention_monthly_months: int = Field(default=3, ge=0, le=120)
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
    proxy_credential_secret: SecretStr | None = None
    gquan_api_base_url: str = "https://one.1oa.com.cn/springboard/api/v1"
    gquan_app_token: SecretStr | None = None
    gquan_delivery_mode: Literal["app", "stub"] = "app"
    gquan_test_code: SecretStr | None = None
    gquan_request_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    seed_default_sites: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
