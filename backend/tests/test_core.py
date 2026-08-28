from datetime import datetime, timezone

from app.services.audit import redact
from app.services.cidr import match_source_ip, normalize_cidr, normalize_source_ip
from app.services.crypto import calculate_bundle_hash, sign_bundle, verify_bundle


def test_bundle_signature_round_trip_is_deterministic() -> None:
    bundle = {
        "schema_version": 1,
        "node_id": "codedev",
        "allow_cidrs": ["10.32.12.0/24"],
        "issued_at": datetime(2026, 8, 28, tzinfo=timezone.utc).isoformat(),
    }
    signed = sign_bundle(bundle, "test-secret")

    assert verify_bundle(signed, "test-secret") == (True, "")
    assert signed["bundle_hash"] == calculate_bundle_hash(signed)

    signed["allow_cidrs"] = ["192.0.2.0/24"]
    assert verify_bundle(signed, "test-secret") == (False, "bundle_hash_mismatch")


def test_cidr_normalization_and_preview_match() -> None:
    cidrs = [normalize_cidr("10.32.12.9/24"), normalize_cidr("2001:db8::1/64")]

    assert cidrs == ["10.32.12.0/24", "2001:db8::/64"]
    assert normalize_source_ip("10.32.12.111") == "10.32.12.111"
    assert match_source_ip("10.32.12.111", cidrs) == "10.32.12.0/24"
    assert match_source_ip("192.0.2.1", cidrs) is None


def test_audit_redaction_covers_nested_node_secrets() -> None:
    value = {
        "token": "one-time-token",
        "nested": {"agent_token": "node-token", "username": "operator"},
    }

    assert redact(value) == {
        "token": "[REDACTED]",
        "nested": {"agent_token": "[REDACTED]", "username": "operator"},
    }
