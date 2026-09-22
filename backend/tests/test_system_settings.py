from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from starlette.requests import Request

import main as main_module
from app.models import ServiceQualityDefinition, SystemSettings
from app.schemas import SystemSettingsUpdate


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/api/v1/system-settings",
            "headers": [],
        }
    )


def test_service_quality_definition_defaults_and_order() -> None:
    assert ServiceQualityDefinition().model_dump() == {
        "green_max_ms": 100,
        "yellow_max_ms": 200,
    }
    assert SystemSettings.model_fields["settings_id"].default == "global"

    with pytest.raises(ValidationError, match="yellow_max_ms_must_exceed_green_max_ms"):
        SystemSettingsUpdate(
            service_quality={"green_max_ms": 200, "yellow_max_ms": 100}
        )


@pytest.mark.asyncio
async def test_update_system_settings_audits_operator_and_new_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[bool] = []
    settings = SimpleNamespace(
        settings_id="global",
        service_quality=ServiceQualityDefinition(),
        updated_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
        updated_by="system",
    )

    async def save() -> None:
        saved.append(True)

    settings.save = save
    audit: dict[str, object] = {}

    async def append_audit(**values: object) -> None:
        audit.update(values)

    async def current_settings() -> SimpleNamespace:
        return settings

    monkeypatch.setattr(main_module, "_global_system_settings", current_settings)
    monkeypatch.setattr(main_module, "append_audit", append_audit)
    monkeypatch.setattr(main_module, "_request_id", lambda _: "request-1")
    monkeypatch.setattr(main_module, "_request_source_ip", lambda _: "127.0.0.1")

    result = await main_module.update_system_settings(
        SystemSettingsUpdate(
            service_quality={"green_max_ms": 80, "yellow_max_ms": 180}
        ),
        _request(),
        "alice",
    )

    assert saved == [True]
    assert result.service_quality.green_max_ms == 80
    assert result.service_quality.yellow_max_ms == 180
    assert settings.updated_by == "alice"
    assert audit["action"] == "system_settings.update"
    assert audit["target_id"] == "global"
    assert audit["request_id"] == "request-1"
    assert audit["source_ip"] == "127.0.0.1"
    assert audit["before"] == {
        "service_quality": {"green_max_ms": 100, "yellow_max_ms": 200},
        "updated_at": "2026-09-22T00:00:00+00:00",
        "updated_by": "system",
    }
    assert audit["after"]["service_quality"] == {
        "green_max_ms": 80,
        "yellow_max_ms": 180,
    }
