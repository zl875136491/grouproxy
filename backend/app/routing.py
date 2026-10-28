import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RoutingRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["domain", "domain_suffix", "ip_cidr"]
    pattern: str = Field(min_length=1, max_length=253)
    action: Literal["direct", "proxy", "reject"]

    @field_validator("pattern")
    @classmethod
    def normalize_pattern(cls, value: str, info) -> str:
        value = value.strip().lower()
        if info.data.get("kind") == "ip_cidr":
            return str(ipaddress.ip_network(value, strict=False))
        value = value.removeprefix(".").rstrip(".")
        value = value.encode("idna").decode("ascii")
        if len(value) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in value.split(".")
        ):
            raise ValueError("invalid routing domain")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return value
        raise ValueError("use ip_cidr for IP addresses")


class RoutingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["rule"] = "rule"
    private_direct: bool = True
    cn_direct: bool = True
    final: Literal["proxy", "direct"] = "proxy"
    rules: list[RoutingRule] = Field(default_factory=list, max_length=200)


class RoutingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    policy: RoutingPolicy


class RoutingPublish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)


class RoutingPolicyOut(BaseModel):
    site_id: str
    revision: int
    policy: RoutingPolicy
