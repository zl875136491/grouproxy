"""Small, durable control-plane alerts for Phase 3 operational state."""

from datetime import timedelta

from ..models import AccessLog, Alert, Node, Site, utcnow


async def set_alert(
    *,
    fingerprint: str,
    category: str,
    title: str,
    detail: str,
    site_id: str = "",
    node_id: str = "",
    severity: str = "warning",
    active: bool,
) -> Alert | None:
    """Open, refresh, or resolve an alert without duplicating a condition."""

    current = utcnow()
    existing = await Alert.find_one(Alert.fingerprint == fingerprint)
    if active:
        if existing is None:
            alert = Alert(
                fingerprint=fingerprint,
                category=category,
                severity=severity,
                site_id=site_id,
                node_id=node_id,
                title=title,
                detail=detail[:512],
                first_seen_at=current,
                last_seen_at=current,
            )
            await alert.insert()
            return alert
        existing.category = category
        existing.severity = severity
        existing.site_id = site_id
        existing.node_id = node_id
        existing.title = title
        existing.detail = detail[:512]
        existing.status = "open"
        existing.last_seen_at = current
        existing.resolved_at = None
        await existing.save()
        return existing
    if existing is not None and existing.status != "resolved":
        existing.status = "resolved"
        existing.resolved_at = current
        existing.last_seen_at = current
        await existing.save()
    return existing


async def sync_node_alerts(node: Node) -> None:
    """Materialize node health dimensions as separately visible alerts."""

    dimensions = (
        (
            "liveness",
            node.last_seen_at is not None and node.liveness_status != "online",
            "critical",
            "Node heartbeat is unavailable",
            node.liveness_status,
        ),
        (
            "configuration",
            node.config_status in {"drift", "failed", "rollback_failed"},
            "critical" if node.config_status == "rollback_failed" else "warning",
            "Node configuration needs attention",
            node.config_status,
        ),
        (
            "service",
            node.service_status in {"unhealthy", "shutdown"},
            "critical" if node.service_status == "unhealthy" else "warning",
            "Node proxy service is unavailable",
            node.service_status,
        ),
        (
            "subscription",
            node.subscription_status in {"refresh_failed", "apply_failed"},
            "warning",
            "Node subscription cannot be applied",
            node.subscription_status,
        ),
    )
    for category, active, severity, title, detail in dimensions:
        await set_alert(
            fingerprint=f"node:{node.agent_id}:{category}",
            category=category,
            title=title,
            detail=detail,
            site_id=node.site_id,
            node_id=node.agent_id,
            severity=severity,
            active=bool(active),
        )


async def refresh_liveness() -> None:
    """Mark missed heartbeats offline while preserving never-seen enrollment."""

    cutoff = utcnow() - timedelta(seconds=45)
    nodes = await Node.find_all().to_list()
    for node in nodes:
        if (
            node.last_seen_at is not None
            and node.last_seen_at < cutoff
            and node.liveness_status != "offline"
        ):
            node.liveness_status = "offline"
            await node.save()
        await sync_node_alerts(node)


async def refresh_deny_spike_alerts() -> None:
    """Detect a sustained deny-rate jump without retaining another metric store."""

    # Keep the thresholds in Settings so an installation can tune them to its
    # normal traffic volume. Import lazily to avoid a config/model import cycle
    # while the application is bootstrapping.
    from ..config import get_settings

    settings = get_settings()
    current = utcnow()
    recent_start = current - timedelta(seconds=settings.deny_spike_window_seconds)
    baseline_start = current - timedelta(seconds=settings.deny_spike_baseline_seconds)
    sites = await Site.find_all().to_list()
    for site in sites:
        site_id = str(site.id)
        recent = await AccessLog.find(
            {
                "site_id": site_id,
                "action": "deny",
                "ts": {"$gte": recent_start},
            }
        ).count()
        baseline = await AccessLog.find(
            {
                "site_id": site_id,
                "action": "deny",
                "ts": {"$gte": baseline_start, "$lt": recent_start},
            }
        ).count()
        # Compare rates, not raw counts, because the baseline is normally
        # longer than the alert window. A threefold jump and a small absolute
        # floor avoid opening alerts on a handful of normal rejects.
        baseline_rate = baseline / max(settings.deny_spike_baseline_seconds, 1)
        recent_rate = recent / max(settings.deny_spike_window_seconds, 1)
        active = recent >= settings.deny_spike_min_events and (
            baseline == 0 or recent_rate >= baseline_rate * 3
        )
        await set_alert(
            fingerprint=f"deny-spike:{site_id}",
            category="deny_spike",
            title="Destination denies increased sharply",
            detail=(
                f"{site.name}: {recent} denies in the last "
                f"{settings.deny_spike_window_seconds}s (baseline {baseline})"
            ),
            site_id=site_id,
            severity="warning",
            active=active,
        )
