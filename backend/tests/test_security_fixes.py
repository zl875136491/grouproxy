"""Tests for security fixes: CORS, CSRF, log redaction, rate limiting."""

import asyncio
import logging
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

# Test CORS configuration


def test_cors_no_wildcard_ports(monkeypatch: pytest.MonkeyPatch):
    """CORS should use strict origin allowlist, not wildcard port regex."""
    from app.config import Settings

    monkeypatch.delenv("GROUPROXY_CORS_ALLOWED_ORIGINS", raising=False)

    # Default development config
    settings = Settings(
        bundle_hmac_secret="a" * 32,
        admin_password="test-password-12",
        management_token="m" * 32,
    )
    
    # Verify CORS origins are comma-separated, not a regex
    assert settings.cors_allowed_origins == "http://localhost:3000,http://127.0.0.1:3000"
    
    # Parse into list
    origins = [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]
    assert len(origins) == 2
    assert "http://localhost:3000" in origins
    assert "http://127.0.0.1:3000" in origins


def test_cors_can_be_disabled():
    """Production deployments can disable CORS by setting empty origins."""
    from app.config import Settings

    settings = Settings(
        bundle_hmac_secret="a" * 32,
        admin_password="test-password-12",
        management_token="m" * 32,
        cors_allowed_origins="",
    )
    
    origins = [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]
    assert len(origins) == 0


# Test log redaction


def test_sensitive_field_filter_redacts_bearer_tokens():
    """Log filter should redact Bearer tokens from messages."""
    # Import after main module sets up logging
    import main
    
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Authorization: Bearer secret_token_12345",
        args=(),
        exc_info=None,
    )
    
    filter_instance = main.SensitiveFieldFilter()
    filter_instance.filter(record)
    
    assert "Bearer [REDACTED]" in record.msg
    assert "secret_token_12345" not in record.msg


def test_sensitive_field_filter_redacts_passwords():
    """Log filter should redact password= patterns."""
    import main
    
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='Login failed: {"password": "secret123"}',
        args=(),
        exc_info=None,
    )
    
    filter_instance = main.SensitiveFieldFilter()
    filter_instance.filter(record)
    
    assert "password=[REDACTED]" in record.msg
    assert "secret123" not in record.msg


def test_sensitive_field_filter_redacts_dict_args():
    """Log filter should redact sensitive dicts in args."""
    import main
    from app.services.audit import redact
    
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="User data: %s",
        args=({"username": "alice", "password": "secret"},),
        exc_info=None,
    )
    
    filter_instance = main.SensitiveFieldFilter()
    filter_instance.filter(record)
    
    # Args should be redacted
    assert record.args[0]["username"] == "alice"
    assert record.args[0]["password"] == "[REDACTED]"


def test_sensitive_field_filter_preserves_named_mapping_args():
    """Named logging placeholders must keep their mapping shape after redaction."""
    import main

    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="User %(username)s password %(password)s",
        args=({"username": "alice", "password": "secret"},),
        exc_info=None,
    )

    main.SensitiveFieldFilter().filter(record)

    assert record.args["password"] == "[REDACTED]"
    assert record.getMessage() == "User alice password [REDACTED]"


def test_csrf_rejection_returns_json_403():
    """A middleware rejection must not escape as a 500 response."""
    import main

    test_app = FastAPI()

    @test_app.post("/mutate")
    async def mutate() -> dict[str, str]:
        return {"status": "ok"}

    test_app.add_middleware(
        main.CSRFProtectionMiddleware,
        allowed_origins=["http://console.example"],
    )
    client = TestClient(test_app)

    rejected = client.post("/mutate", headers={"Origin": "http://evil.example"})
    allowed = client.post("/mutate", headers={"Origin": "http://console.example"})

    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "csrf_origin_mismatch"}
    assert allowed.status_code == 200


# Test rate limiting


@pytest.mark.asyncio
async def test_rate_limiter_basic():
    """Rate limiter should track requests and enforce limits."""
    import main
    
    limiter = main.RateLimiter()
    
    # Allow first 5 requests (limit=5, window=60)
    for i in range(5):
        allowed, remaining = await limiter.check_rate_limit("test_key", 5, 60)
        assert allowed is True
        assert remaining == 5 - i - 1
    
    # 6th request should be blocked
    allowed, remaining = await limiter.check_rate_limit("test_key", 5, 60)
    assert allowed is False
    assert remaining == 0


@pytest.mark.asyncio
async def test_rate_limiter_different_keys():
    """Rate limiter should track different keys independently."""
    import main
    
    limiter = main.RateLimiter()
    
    # Use up limit for key1
    for _ in range(3):
        await limiter.check_rate_limit("key1", 3, 60)
    
    # key1 should be blocked
    allowed, _ = await limiter.check_rate_limit("key1", 3, 60)
    assert allowed is False
    
    # key2 should still work
    allowed, _ = await limiter.check_rate_limit("key2", 3, 60)
    assert allowed is True


@pytest.mark.asyncio
async def test_rate_limiter_window_expiry():
    """Rate limiter should expire old requests outside window."""
    import main
    
    limiter = main.RateLimiter()
    
    # Make requests with immediate expiry (1 second window)
    await limiter.check_rate_limit("test_key", 2, 1)
    
    # Wait for window to expire
    await asyncio.sleep(1.1)
    
    # Should allow new requests after window expires
    allowed, remaining = await limiter.check_rate_limit("test_key", 2, 1)
    assert allowed is True
    assert remaining == 1


@pytest.mark.asyncio
async def test_rate_limiter_cleanup():
    """Rate limiter should clean up stale windows."""
    import main
    
    limiter = main.RateLimiter()
    
    # Create some windows
    await limiter.check_rate_limit("key1", 10, 60)
    await limiter.check_rate_limit("key2", 10, 60)
    
    assert len(limiter._windows) == 2
    
    # Manually set old timestamps to simulate stale data
    import time
    old_time = time.time() - 3700  # Over 1 hour ago
    limiter._windows["key1"] = [old_time]
    limiter._windows["key2"] = [old_time]
    
    # Cleanup should remove stale windows
    await limiter.cleanup_old_windows()
    
    assert len(limiter._windows) == 0


# Integration test hints for manual verification


def test_csrf_protection_hint():
    """CSRF protection is middleware-based. Manual test:
    
    1. Start backend with CORS enabled
    2. Attempt POST to /api/v1/sites without Origin header
    3. Should receive 403 csrf_origin_mismatch
    4. Attempt POST with Origin: http://localhost:3000
    5. Should succeed (if authenticated)
    6. Agent endpoints /agent/v1/* should NOT require Origin check
    """
    pass


def test_rate_limit_hint():
    """Rate limiting is middleware-based. Manual test:
    
    1. Start backend
    2. Call POST /api/v1/auth/login/password 11 times rapidly
    3. 11th request should return 429 rate_limit_exceeded
    4. Wait 60 seconds, should work again
    5. Agent endpoints have higher limits (1000 req/min)
    """
    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
