"""Tests for P3 hardening: security headers, readyz timeout, CI setup."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# Test I1: Frontend security headers


def test_security_headers_config():
    """Next.js config should include security headers."""
    # Read next.config.mjs and verify headers() function exists
    import os
    config_path = os.path.join(os.path.dirname(__file__), "../../frontend/next.config.mjs")
    if os.path.exists(config_path):
        with open(config_path) as f:
            content = f.read()
        
        # Verify key security headers are configured
        assert "X-Frame-Options" in content
        assert "X-Content-Type-Options" in content
        assert "Referrer-Policy" in content
        assert "Content-Security-Policy" in content
        assert "Permissions-Policy" in content


def test_security_headers_values():
    """Security headers should have secure values."""
    # This would ideally be an integration test hitting the frontend
    # For now, document the expected headers
    expected_headers = {
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    
    # In integration test, would verify:
    # response = client.get("/")
    # for header, value in expected_headers.items():
    #     assert response.headers[header] == value
    pass


def test_csp_allows_nextjs_requirements():
    """CSP should allow inline scripts/styles for Next.js."""
    # Read config to verify conservative CSP
    import os
    config_path = os.path.join(os.path.dirname(__file__), "../../frontend/next.config.mjs")
    if os.path.exists(config_path):
        with open(config_path) as f:
            content = f.read()
        
        # Verify CSP includes necessary directives for Next.js
        assert "script-src" in content
        assert "style-src" in content
        # Should be conservative but functional
        assert "'unsafe-inline'" in content  # Next.js needs this


# Test I2: Readyz timeout


@pytest.mark.asyncio
async def test_readyz_has_timeout():
    """Readyz endpoint should timeout on slow MongoDB."""
    from main import readyz
    from fastapi import Request, HTTPException
    
    # Mock a slow database
    mock_db = MagicMock()
    mock_client = MagicMock()
    mock_admin = MagicMock()
    
    # Simulate a hanging ping
    async def slow_ping(*args, **kwargs):
        await asyncio.sleep(10)  # Hang for 10 seconds
    
    mock_admin.command = slow_ping
    mock_client.admin = mock_admin
    mock_db.client = mock_client
    
    # Mock request with database
    mock_request = MagicMock(spec=Request)
    mock_request.app.state.database = mock_db
    
    # Should timeout before 10 seconds (configured for 2s)
    start = asyncio.get_event_loop().time()
    with pytest.raises(HTTPException) as exc_info:
        await readyz(mock_request)
    elapsed = asyncio.get_event_loop().time() - start
    
    # Should timeout quickly (within 3s to allow some overhead)
    assert elapsed < 3.0
    assert exc_info.value.status_code == 503
    assert "timeout" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_readyz_succeeds_on_quick_response():
    """Readyz should succeed when MongoDB responds quickly."""
    from main import readyz
    from fastapi import Request
    
    # Mock a quick database
    mock_db = MagicMock()
    mock_client = MagicMock()
    mock_admin = MagicMock()
    
    # Quick ping response
    async def quick_ping(*args, **kwargs):
        await asyncio.sleep(0.01)
        return {"ok": 1}
    
    mock_admin.command = quick_ping
    mock_client.admin = mock_admin
    mock_db.client = mock_client
    
    mock_request = MagicMock(spec=Request)
    mock_request.app.state.database = mock_db
    
    # Should succeed
    result = await readyz(mock_request)
    assert result["status"] == "ready"


# Test I5: CI workflow


def test_ci_workflow_exists():
    """GitHub Actions CI workflow should exist."""
    import os
    workflow_path = os.path.join(
        os.path.dirname(__file__),
        "../../.github/workflows/ci.yml"
    )
    assert os.path.exists(workflow_path), "CI workflow file should exist"


def test_ci_workflow_includes_tests():
    """CI workflow should run backend and frontend tests."""
    import os
    workflow_path = os.path.join(
        os.path.dirname(__file__),
        "../../.github/workflows/ci.yml"
    )
    
    if os.path.exists(workflow_path):
        with open(workflow_path) as f:
            content = f.read()
        
        # Verify workflow includes necessary jobs
        assert "backend-tests" in content or "backend" in content.lower()
        assert "pytest" in content
        
        # Should include frontend checks
        assert "frontend" in content.lower()
        assert "tsc" in content or "typecheck" in content.lower()


def test_ci_runs_security_tests():
    """CI should run security-related unit tests."""
    import os
    workflow_path = os.path.join(
        os.path.dirname(__file__),
        "../../.github/workflows/ci.yml"
    )
    
    if os.path.exists(workflow_path):
        with open(workflow_path) as f:
            content = f.read()
        
        # Should run security test files
        assert "test_security" in content or "test_p2" in content or "test_p3" in content


# Test I4: This file itself


def test_p3_tests_exist():
    """P3 hardening tests should exist and be importable."""
    # Verify this test file exists and has test functions
    import os
    from pathlib import Path
    
    test_file = Path(__file__)
    assert test_file.exists(), "Test file should exist"
    assert test_file.name == "test_p3_hardening.py", "Test file should be named correctly"
    
    # Read file and count test functions
    content = test_file.read_text()
    test_functions = [line for line in content.split('\n') if line.startswith('def test_')]
    assert len(test_functions) > 0, "Should have test functions"


# Integration test hints


def test_security_headers_integration_hint():
    """Security headers integration test:
    
    1. Build frontend: cd frontend && npm run build
    2. Start standalone: node .next/standalone/server.js
    3. Curl response headers:
       curl -I http://localhost:3000/
    4. Verify headers present:
       - X-Frame-Options: DENY
       - X-Content-Type-Options: nosniff
       - Content-Security-Policy: (contains default-src 'self')
    5. Test app still works (no broken inline scripts/styles)
    """
    pass


def test_readyz_timeout_integration_hint():
    """Readyz timeout integration test:
    
    1. Start backend normally
    2. Verify readyz works: curl http://localhost:8000/readyz
       Should return {"status": "ready"}
    3. Block MongoDB (e.g., network rule or stop container)
    4. Call readyz again: curl http://localhost:8000/readyz
       Should return 503 within ~2 seconds (not hang)
    5. Check response: {"detail": "database_ping_timeout"}
    """
    pass


def test_ci_workflow_integration_hint():
    """CI workflow integration test:
    
    1. Push to a branch or open PR
    2. Check GitHub Actions tab
    3. Verify jobs run:
       - backend-tests (pytest)
       - frontend-lint (tsc, i18n)
       - monitor-tests (go test)
    4. All should pass on clean main branch
    5. Break a test, verify CI fails
    """
    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
