import hashlib
import hmac
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Return the cross-language canonical JSON representation used by Bundle signing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def bundle_hash_input(bundle: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in bundle.items() if key not in {"bundle_hash", "mac"}}


def calculate_bundle_hash(bundle: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(bundle_hash_input(bundle))).hexdigest()


def sign_bundle(bundle: dict[str, Any], secret: str) -> dict[str, Any]:
    signed = dict(bundle)
    signed["bundle_hash"] = calculate_bundle_hash(signed)
    unsigned_mac = {key: value for key, value in signed.items() if key != "mac"}
    signed["mac"] = hmac.new(
        secret.encode("utf-8"), canonical_json(unsigned_mac), hashlib.sha256
    ).hexdigest()
    return signed


def verify_bundle(bundle: dict[str, Any], secret: str) -> tuple[bool, str]:
    supplied_hash = str(bundle.get("bundle_hash", ""))
    expected_hash = calculate_bundle_hash(bundle)
    if not hmac.compare_digest(supplied_hash, expected_hash):
        return False, "bundle_hash_mismatch"
    supplied_mac = str(bundle.get("mac", ""))
    unsigned_mac = {key: value for key, value in bundle.items() if key != "mac"}
    expected_mac = hmac.new(
        secret.encode("utf-8"), canonical_json(unsigned_mac), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(supplied_mac, expected_mac):
        return False, "bundle_mac_mismatch"
    return True, ""
