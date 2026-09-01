"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleAlert, Gauge, RefreshCw, Server, Waypoints } from "lucide-react";
import { useMemo, useState } from "react";
import { getNodes, getProxyConfigs, getSites, type Node, type ProxyEndpoint, type ProxyGroup, type ProxyConfigSnapshot } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { cn } from "../../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { IconButton, Panel, StatusBadge } from "../../components/ui";

function endpointStatus(endpoint: ProxyEndpoint): string {
  if (endpoint.alive === false) return "offline";
  if (endpoint.delay_ms === null || endpoint.delay_ms === undefined) return "pending";
  if (endpoint.delay_ms <= 250) return "online";
  if (endpoint.delay_ms <= 800) return "degraded";
  return "attention";
}

function proxyTypeLabel(value: string, t: (key: string) => string): string {
  const normalized = value.trim().toLowerCase().replaceAll("_", "-");
  const labels: Record<string, string> = {
    selector: "Selector",
    fallback: "Fallback",
    "url-test": "URL test",
    urltest: "URL test",
    direct: "Direct",
    reject: "Reject",
    block: "Reject",
    trojan: "Trojan",
    shadowsocks: "Shadowsocks",
    vmess: "VMess",
    vless: "VLESS",
    http: "HTTP",
    socks: "SOCKS5",
    socks5: "SOCKS5",
  };
  const key = labels[normalized];
  return key ? t(key) : value;
}

function groupStatus(group: ProxyGroup): string {
  if (!group.nodes.length) return "pending";
  if (group.nodes.every((endpoint) => endpointStatus(endpoint) === "offline")) return "offline";
  if (group.nodes.some((endpoint) => endpointStatus(endpoint) === "online")) return "online";
  return "degraded";
}

function latencyHistoryTone(delay: number | null): string {
  if (delay === null || delay === undefined) return "unknown";
  if (delay <= 250) return "good";
  if (delay <= 800) return "slow";
  return "bad";
}

function LatencyHistory({ history, formatDuration, t }: { history: ProxyEndpoint["history"]; formatDuration: (value: number | null | undefined) => string; t: (key: string, values?: Record<string, string | number>) => string }) {
  const points = history.slice(-10);
  return <div className="proxy-history" aria-label={t("Recent latency history")}>{points.length ? points.map((point, index) => {
    const label = point.delay_ms === null || point.delay_ms === undefined ? t("No latency") : formatDuration(point.delay_ms);
    return <span className={cn("proxy-history-dot", `proxy-history-${latencyHistoryTone(point.delay_ms)}`)} key={`${point.at || "point"}-${index}`} title={label} aria-label={label} />;
  }) : null}</div>;
}

function SnapshotState({ snapshot, formatDate, t }: { snapshot: ProxyConfigSnapshot | undefined; formatDate: (value: string | null | undefined, withTime?: boolean) => string; t: (key: string, values?: Record<string, string | number>) => string }) {
  if (!snapshot) return <StatusBadge status="pending" />;
  if (!snapshot.api_available) return <StatusBadge status="offline" />;
  const sampled = new Date(snapshot.sampled_at).getTime();
  const stale = Number.isFinite(sampled) && Date.now() - sampled > 90_000;
  return <span className="proxy-snapshot-state"><StatusBadge status={stale ? "degraded" : "online"} /><small>{t("Updated {date}", { date: formatDate(snapshot.sampled_at) })}</small></span>;
}

function EndpointCard({ endpoint, selected, formatDuration, t }: { endpoint: ProxyEndpoint; selected: boolean; formatDuration: (value: number | null | undefined) => string; t: (key: string, values?: Record<string, string | number>) => string }) {
  const status = endpointStatus(endpoint);
  const delay = endpoint.delay_ms === null || endpoint.delay_ms === undefined ? t("No latency") : formatDuration(endpoint.delay_ms);
  return <div className={cn("proxy-endpoint-card", selected && "proxy-endpoint-selected", `proxy-endpoint-${status}`)}>
    <div className="proxy-endpoint-heading"><span className="proxy-endpoint-dot" aria-hidden="true" /><strong title={endpoint.name}>{endpoint.name}</strong>{selected ? <span className="proxy-current-mark">{t("Current")}</span> : null}</div>
    <div className="proxy-endpoint-meta"><span>{proxyTypeLabel(endpoint.type, t)}</span><span>{endpoint.udp ? "UDP" : "TCP"}</span><StatusBadge status={status} /></div>
    <span className="proxy-endpoint-delay">{delay}</span>
    <LatencyHistory history={endpoint.history} formatDuration={formatDuration} t={t} />
  </div>;
}

function ProxyGroupCard({ group, formatDuration, t }: { group: ProxyGroup; formatDuration: (value: number | null | undefined) => string; t: (key: string, values?: Record<string, string | number>) => string }) {
  const endpointByName = new Map(group.nodes.map((endpoint) => [endpoint.name, endpoint]));
  const endpoints = group.all.map((name) => endpointByName.get(name) || { name, type: "unknown", udp: false, alive: null, delay_ms: null, history: [] });
  return <article className="proxy-group-card">
    <header className="proxy-group-heading"><div className="proxy-group-title"><span className="proxy-group-icon" aria-hidden="true"><Waypoints size={16} /></span><div><strong>{group.name}</strong><span>{proxyTypeLabel(group.type, t)} · {t("{count} endpoints", { count: endpoints.length })}</span></div></div><StatusBadge status={groupStatus(group)} /></header>
    <div className="proxy-group-current"><span className="proxy-current-label">{t("Selected")}</span><strong>{group.now || t("No selection")}</strong>{group.delay_ms !== null && group.delay_ms !== undefined ? <span className="proxy-group-delay">{formatDuration(group.delay_ms)}</span> : null}</div>
    <div className="proxy-endpoint-grid">{endpoints.map((endpoint) => <EndpointCard key={endpoint.name} endpoint={endpoint} selected={Boolean(group.now && endpoint.name === group.now)} formatDuration={formatDuration} t={t} />)}</div>
  </article>;
}

function NodeProxyPanel({ node, snapshot, formatDate, formatDuration, formatNumber, t }: { node: Node; snapshot: ProxyConfigSnapshot | undefined; formatDate: (value: string | null | undefined, withTime?: boolean) => string; formatDuration: (value: number | null | undefined) => string; formatNumber: (value: number | null | undefined) => string; t: (key: string, values?: Record<string, string | number>) => string }) {
  return <Panel className="proxy-node-panel">
    <header className="proxy-node-heading"><div className="proxy-node-title"><span className="proxy-node-icon" aria-hidden="true"><Server size={17} /></span><div><h2>{node.name}</h2><span className="mono">{node.agent_id}</span></div></div><SnapshotState snapshot={snapshot} formatDate={formatDate} t={t} /></header>
    {snapshot?.error ? <div className="proxy-inline-warning"><CircleAlert size={15} /><span>{t(snapshot.error)}</span></div> : null}
    {snapshot?.groups.length ? <div className={cn("proxy-groups", !snapshot.api_available && "proxy-groups-stale")}>
      {!snapshot.api_available ? <div className="proxy-stale-note">{t("Showing last successful snapshot.")}</div> : null}
      {snapshot.groups.map((group) => <ProxyGroupCard key={group.name} group={group} formatDuration={formatDuration} t={t} />)}
    </div> : <EmptyState title={snapshot?.api_available ? "No proxy groups reported." : "Proxy API is unavailable."} detail={snapshot?.api_available ? "The monitor has not reported a selectable group yet." : "The control plane will show the last successful snapshot when the monitor reconnects."} />}
    <footer className="proxy-node-footer"><span>{t("{count} groups", { count: formatNumber(snapshot?.groups.length || 0) })}</span><span>{snapshot ? t("Received {date}", { date: formatDate(snapshot.received_at) }) : t("Awaiting monitor data")}</span></footer>
  </Panel>;
}

export default function ProxiesPage() {
  const { t, formatDate, formatDuration, formatNumber } = usePreferences();
  const session = useManagementSession();
  const [siteFilter, setSiteFilter] = useState("all");
  const configs = useQuery({ queryKey: ["proxy-configs"], queryFn: () => getProxyConfigs(), enabled: session === true, refetchInterval: 5_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, staleTime: 10_000 });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  const snapshots = useMemo(() => new Map((configs.data || []).map((snapshot) => [snapshot.node_id, snapshot])), [configs.data]);
  const filteredNodes = useMemo(() => (nodes.data || []).filter((node) => siteFilter === "all" || node.site_id === siteFilter), [nodes.data, siteFilter]);

  if (session === null) return <LoadingState rows={8} />;
  if (!session) return <SessionGate />;
  if (configs.isLoading || nodes.isLoading || sites.isLoading) return <LoadingState rows={8} />;
  if (configs.isError || nodes.isError || sites.isError) {
    const issue = configs.error || nodes.error || sites.error;
    return <ErrorState error={issue instanceof Error ? issue.message : "Unable to load proxy configuration."} onRetry={() => void Promise.all([configs.refetch(), nodes.refetch(), sites.refetch()])} />;
  }

  const siteItems = sites.data || [];
  const availableSnapshots = (configs.data || []).length;
  return <div className="page-stack">
    <PageHeader eyebrow="OPERATE" title="Proxy configuration" description="Read-only outbound groups reported by each regional monitor. Endpoint credentials and server details never leave the node." actions={<IconButton label={t("Refresh")} onClick={() => void Promise.all([configs.refetch(), nodes.refetch()])}><RefreshCw size={16} /></IconButton>} />
    <Panel className="proxy-toolbar-panel"><div className="table-toolbar"><div className="toolbar-title"><Gauge size={18} /><span>{t("{count} regional snapshots", { count: formatNumber(availableSnapshots) })}</span></div><div className="segmented-control" role="tablist" aria-label={t("Filter by site")}><button type="button" className={siteFilter === "all" ? "segmented-active" : ""} role="tab" aria-selected={siteFilter === "all"} onClick={() => setSiteFilter("all")}>{t("All sites")}</button>{siteItems.map((site) => <button type="button" className={siteFilter === site.id ? "segmented-active" : ""} role="tab" aria-selected={siteFilter === site.id} onClick={() => setSiteFilter(site.id)} key={site.id}>{t(site.name)}</button>)}</div></div><p className="proxy-readonly-note">{t("Snapshots are refreshed by monitors; selecting a node here does not change its live outbound.")}</p></Panel>
    {filteredNodes.length ? <div className="proxy-node-list">{filteredNodes.map((node) => { const site = siteItems.find((item) => item.id === node.site_id); return <section className="proxy-site-section" key={node.id}><div className="proxy-site-heading"><div><span className="page-eyebrow">{t("REGIONAL TOPOLOGY")}</span><h2>{t(site?.name || node.site_id)}</h2></div><span className="proxy-site-slug mono">{site?.slug || node.site_id}</span></div><NodeProxyPanel node={node} snapshot={snapshots.get(node.agent_id)} formatDate={formatDate} formatDuration={formatDuration} formatNumber={formatNumber} t={t} /></section>; })}</div> : <Panel><EmptyState title="No nodes enrolled." detail="Enroll a regional monitor before viewing proxy groups." /></Panel>}
  </div>;
}
