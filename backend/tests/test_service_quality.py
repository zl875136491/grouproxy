from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.schemas import ProxyEndpointSnapshot, ProxyGroupSnapshot, ProxyHistoryPoint
import main as main_module


def test_proxy_quality_samples_keep_latest_success_and_report_current_failure() -> None:
    now = datetime(2026, 9, 22, 3, 0, tzinfo=timezone.utc)
    group = ProxyGroupSnapshot(
        name="subscription",
        type="Selector",
        all=["edge-a", "edge-b", "edge-c"],
        nodes=[
            ProxyEndpointSnapshot(
                name="edge-a",
                delay_ms=120,
                alive=True,
                history=[
                    ProxyHistoryPoint(at=now - timedelta(minutes=2), delay_ms=100),
                    ProxyHistoryPoint(at=now - timedelta(minutes=1), delay_ms=120),
                ],
            ),
            ProxyEndpointSnapshot(
                name="edge-b",
                delay_ms=230,
                alive=False,
                history=[ProxyHistoryPoint(at=now - timedelta(minutes=2), delay_ms=230)],
            ),
            ProxyEndpointSnapshot(name="edge-c", delay_ms=80, alive=None),
        ],
    )

    samples = main_module._proxy_quality_samples(
        node=SimpleNamespace(agent_id="tj-s3-proxy", site_id="site-north"),
        groups=[group],
        sampled_at=now,
        received_at=now,
    )

    by_tag = {sample["outbound_tag"]: sample for sample in samples if sample["success"]}
    assert by_tag["edge-a"]["delay_ms"] == 120
    assert by_tag["edge-a"]["sampled_at"] == now - timedelta(minutes=1)
    assert by_tag["edge-c"]["delay_ms"] == 80
    assert by_tag["edge-c"]["sampled_at"] == now

    failed = [sample for sample in samples if sample["outbound_tag"] == "edge-b" and not sample["success"]]
    assert len(failed) == 1
    assert failed[0]["delay_ms"] is None
    assert failed[0]["sampled_at"] == now


def test_proxy_quality_samples_ignore_non_subscription_groups() -> None:
    now = datetime(2026, 9, 22, 3, 0, tzinfo=timezone.utc)
    group = ProxyGroupSnapshot(
        name="GLOBAL",
        type="Selector",
        all=["edge-a"],
        nodes=[ProxyEndpointSnapshot(name="edge-a", delay_ms=100, alive=True)],
    )

    samples = main_module._proxy_quality_samples(
        node=SimpleNamespace(agent_id="codedev", site_id="site-east"),
        groups=[group],
        sampled_at=now,
        received_at=now,
    )

    assert samples == []
