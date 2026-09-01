"""Probe history persistence and circuit-breaker transitions."""

from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from ..models import Node, ProbeCircuit, ProbeHistory, utcnow
from .alerts import set_alert

FAILURE_THRESHOLD = 3
RECOVERY_SUCCESS_THRESHOLD = 2
COOLDOWN = timedelta(seconds=60)
HISTORY_RETENTION = timedelta(days=14)


async def record_probe_result(
    *,
    node: Node,
    batch_id: str,
    outbound_tag: str,
    target_url: str,
    success: bool,
    latency_ms: int,
    error_class: str,
    sampled_at,
) -> ProbeCircuit:
    """Append a probe sample and atomically advance its per-outbound circuit."""

    current = utcnow()
    await ProbeHistory(
        node_id=node.agent_id,
        site_id=node.site_id,
        batch_id=batch_id,
        outbound_tag=outbound_tag,
        target_url=target_url,
        success=success,
        latency_ms=latency_ms,
        error_class=error_class,
        sampled_at=sampled_at,
        expires_at=sampled_at + HISTORY_RETENTION,
    ).insert()
    circuit = await ProbeCircuit.find_one(
        ProbeCircuit.node_id == node.agent_id,
        ProbeCircuit.outbound_tag == outbound_tag,
    )
    if circuit is None:
        circuit = ProbeCircuit(
            node_id=node.agent_id,
            site_id=node.site_id,
            outbound_tag=outbound_tag,
        )

    if (
        circuit.state == "open"
        and circuit.opened_at is not None
        and current - circuit.opened_at >= COOLDOWN
    ):
        circuit.state = "half_open"
        circuit.half_open_at = current
        circuit.consecutive_successes = 0
        circuit.reason = "cooldown_elapsed"

    if success:
        circuit.last_success_at = current
        circuit.last_latency_ms = latency_ms
        circuit.last_error_class = ""
        circuit.consecutive_failures = 0
        circuit.consecutive_successes += 1
        if (
            circuit.state == "half_open"
            and circuit.consecutive_successes >= RECOVERY_SUCCESS_THRESHOLD
        ):
            circuit.state = "closed"
            circuit.opened_at = None
            circuit.half_open_at = None
            circuit.reason = "recovery_success_threshold"
    else:
        circuit.last_failure_at = current
        circuit.last_latency_ms = latency_ms
        circuit.last_error_class = error_class or "probe_failed"
        circuit.consecutive_successes = 0
        circuit.consecutive_failures += 1
        if circuit.state == "half_open" or (
            circuit.state == "closed" and circuit.consecutive_failures >= FAILURE_THRESHOLD
        ):
            circuit.state = "open"
            circuit.opened_at = current
            circuit.half_open_at = None
            circuit.reason = (
                "half_open_probe_failed"
                if circuit.consecutive_failures < FAILURE_THRESHOLD
                else "failure_threshold"
            )

    circuit.updated_at = current
    if circuit.id is None:
        try:
            await circuit.insert()
        except DuplicateKeyError:
            circuit = await ProbeCircuit.find_one(
                ProbeCircuit.node_id == node.agent_id,
                ProbeCircuit.outbound_tag == outbound_tag,
            )
            if circuit is None:
                raise
    else:
        await circuit.save()

    await set_alert(
        fingerprint=f"probe:{node.agent_id}:{outbound_tag}",
        category="probe",
        title="Outbound circuit is open",
        detail=f"{outbound_tag}: {circuit.reason or circuit.last_error_class}",
        site_id=node.site_id,
        node_id=node.agent_id,
        severity="warning",
        active=circuit.state == "open",
    )
    has_open = await ProbeCircuit.find_one(
        ProbeCircuit.node_id == node.agent_id,
        ProbeCircuit.state == "open",
    )
    node.probe_status = (
        "open" if has_open else ("closed" if circuit.state == "closed" else "half_open")
    )
    await node.save()
    return circuit
