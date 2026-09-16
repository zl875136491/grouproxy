"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, ArrowRight, Ban } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  getConnections,
  getNodes,
  getOverview,
  getReleases,
  getSites,
  type ConnectionSnapshot,
  type Node,
  type Site,
} from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { ControlPlaneTopology } from "../../components/control-plane-topology";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Panel, StatusBadge } from "../../components/ui";
import { notifyToast } from "../../components/toast";

const activeReleaseStates = new Set(["queued", "applying", "health_check", "rolling_back"]);

type TrafficSeriesPoint = {
  at: string;
  tx_bps: number;
  rx_bps: number;
  active_connections: number;
};

type TrafficMetricKey = keyof Pick<TrafficSeriesPoint, "tx_bps" | "rx_bps" | "active_connections">;

function siteState(site: Site, nodes: Node[]) {
  if (site.shutdown) return "shutdown";
  if (!nodes.length) return "not enrolled";
  if (nodes.some((node) => node.config_status === "rollback_failed" || node.service_status === "unhealthy")) {
    return "attention";
  }
  if (nodes.some((node) => node.liveness_status !== "online" || node.config_status !== "in_sync")) {
    return "degraded";
  }
  return "healthy";
}

export default function OverviewPage() {
  const { locale, t, formatDate, formatNumber, formatBytes } = usePreferences();
  const session = useManagementSession();
  const overview = useQuery({ queryKey: ["overview"], queryFn: getOverview, enabled: session === true, refetchInterval: 10_000 });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, refetchInterval: 10_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, refetchInterval: 10_000 });
  const releases = useQuery({ queryKey: ["releases"], queryFn: () => getReleases(), enabled: session === true, refetchInterval: 5_000 });
  const traffic = useQuery({
    queryKey: ["overview-traffic"],
    queryFn: () => getConnections({ since: new Date(Date.now() - 60 * 60 * 1000).toISOString(), limit: 250 }),
    enabled: session === true,
    refetchInterval: 10_000,
  });
  const lastReportedDrift = useRef<number | null>(null);
  const [selectedSiteId, setSelectedSiteId] = useState("");

  useEffect(() => {
    const availableSites = sites.data || [];
    const availableNodes = nodes.data || [];
    const preferredSite = availableSites.find((site) => availableNodes.some((node) => node.site_id === site.id)) || availableSites[0];
    if (!selectedSiteId && sites.data && nodes.data && preferredSite) setSelectedSiteId(preferredSite.id);
    if (selectedSiteId && !availableSites.some((site) => site.id === selectedSiteId)) setSelectedSiteId(preferredSite?.id || "");
  }, [nodes.data, selectedSiteId, sites.data]);

  useEffect(() => {
    const driftedNodes = overview.data?.drifted_nodes || 0;
    if (driftedNodes > 0 && lastReportedDrift.current !== driftedNodes) {
      notifyToast({
        title: t("{count} nodes need attention", { count: formatNumber(driftedNodes) }),
        description: t("Configuration or service state differs from the desired release."),
        variant: "destructive",
      });
    }
    lastReportedDrift.current = driftedNodes;
  }, [formatNumber, overview.data?.drifted_nodes, t]);

  if (session === null) return <LoadingState rows={8} />;
  if (!session) return <SessionGate />;
  if (overview.isLoading || sites.isLoading || nodes.isLoading) return <LoadingState rows={8} />;
  if (overview.isError || sites.isError || nodes.isError) {
    const issue = overview.error || sites.error || nodes.error;
    return <ErrorState error={issue instanceof Error ? issue.message : "The control plane did not respond."} onRetry={() => void Promise.all([overview.refetch(), sites.refetch(), nodes.refetch()])} />;
  }

  if (!overview.data) return <LoadingState rows={8} />;

  const siteItems = sites.data || [];
  const nodeItems = nodes.data || [];
  const overviewData = overview.data;
  const activeReleases = (releases.data || []).filter((item) => activeReleaseStates.has(item.status));
  const nodesBySite = new Map(siteItems.map((site) => [site.id, nodeItems.filter((node) => node.site_id === site.id)]));
  const topologySites = siteItems.map((site) => {
    const siteNodes = nodesBySite.get(site.id) || [];
    return { site, state: siteState(site, siteNodes), nodeCount: siteNodes.length };
  });
  const selectedSite = siteItems.find((site) => site.id === selectedSiteId) || null;
  const selectedSiteNodes = selectedSite ? (nodesBySite.get(selectedSite.id) || []) : [];
  const siteTraffic = selectedSite ? buildSiteTrafficSeries(traffic.data || [], selectedSite.id) : [];
  const fallbackTraffic = selectedSiteNodes.reduce(
    (totals, node) => ({
      tx_bps: totals.tx_bps + (node.tx_bps || 0),
      rx_bps: totals.rx_bps + (node.rx_bps || 0),
      active_connections: totals.active_connections + (node.active_connections || 0),
    }),
    { tx_bps: 0, rx_bps: 0, active_connections: 0 },
  );
  const chartSeries = siteTraffic.length
    ? siteTraffic
    : selectedSiteNodes.length
      ? [{ at: new Date().toISOString(), ...fallbackTraffic }]
      : [];

  return (
    <div className="overview-page">
      <h1 className="sr-only">{t("Overview")}</h1>

      <section className="overview-summary-strip" aria-label={t("Control-plane summary")}>
        <OverviewMetric
          label={t("Nodes online")}
          value={<><span>{formatNumber(overviewData.online_nodes)}</span><small> / {formatNumber(overviewData.nodes)}</small></>}
          detail={t("Heartbeat state")}
        />
        <OverviewMetric
          label={t("Configuration in sync")}
          value={<><span>{formatNumber(overviewData.in_sync_nodes)}</span><small> / {formatNumber(overviewData.nodes)}</small></>}
          detail={t("Applied bundle matches")}
        />
        <OverviewMetric
          label={t("Active connections")}
          value={formatNumber(overviewData.connections)}
          detail={t("sing-box live sessions")}
        />
        <OverviewMetric
          label={t("Traffic overview")}
          value={<><span>{formatBytes(overviewData.tx_bps || 0)}<small>/s</small></span><span className="overview-value-divider"> / </span><span>{formatBytes(overviewData.rx_bps || 0)}<small>/s</small></span></>}
          detail={`${t("Upload rate")} / ${t("Download rate")}`}
        />
        <OverviewMetric
          label={t("Drift or failure")}
          value={formatNumber(overviewData.drifted_nodes)}
          detail={t("Requires operator review")}
          tone={overviewData.drifted_nodes > 0 ? "danger" : undefined}
        />
        <OverviewMetric
          label={t("Active deployments")}
          value={formatNumber(activeReleases.length)}
          detail={t("Queued and running releases")}
        />
        <OverviewMetric
          label={t("Open alerts")}
          value={<><span>{formatNumber(overviewData.open_alerts)}</span><span className="overview-value-divider"> / </span><span>{formatNumber(overviewData.open_circuits)}</span></>}
          detail={`${t("Open alerts")} / ${t("Open circuits")}`}
          tone={overviewData.open_alerts > 0 || overviewData.open_circuits > 0 ? "danger" : undefined}
        />
      </section>

      <section className="overview-workspace" aria-label={t("Regional proxy state and deployment activity.")}>
        <Panel className="topology-panel overview-topology-panel">
          <div className="panel-heading">
            <div><span className="panel-kicker">{t("REGIONAL TOPOLOGY")}</span><h2>{t("Control plane to edge sites")}</h2></div>
            <Link href="/nodes">{t("Node inventory")} <ArrowRight size={15} /></Link>
          </div>
          <ControlPlaneTopology
            sites={topologySites}
            onlineNodes={overviewData.online_nodes}
            totalNodes={overviewData.nodes}
            formatNumber={formatNumber}
            t={t}
            selectedSiteId={selectedSiteId}
            onSiteSelect={setSelectedSiteId}
            orientation="vertical"
            fillContainer
          />
          {selectedSite ? <TopologySiteDetail site={selectedSite} nodes={selectedSiteNodes} state={topologySites.find((item) => item.site.id === selectedSite.id)?.state || "unknown"} formatNumber={formatNumber} formatBytes={formatBytes} t={t} /> : <div className="topology-site-empty">{t("Select a site in the topology to inspect its nodes.")}</div>}
        </Panel>

        <section className="traffic-overview overview-traffic-section" aria-label={t("Traffic overview")}>
          <div className="overview-traffic-heading">
            <div className="overview-traffic-heading-copy">
              <div className="toolbar-title"><Activity size={18} /><span>{t("Traffic overview")}</span></div>
              <span className="toolbar-note">{selectedSite ? t(selectedSite.name) : t("No node enrolled")} · {t("{count} nodes", { count: formatNumber(selectedSiteNodes.length) })}</span>
            </div>
            <div className="page-action-group">
              <Link href="/blacklist"><Ban size={15} /> {t("Manage blacklist")}</Link>
              <Link href="/connections">{t("Live connections")} <ArrowRight size={15} /></Link>
            </div>
          </div>
          {traffic.isLoading ? <Panel className="traffic-chart-state"><LoadingState rows={3} /></Panel> : traffic.isError ? <ErrorState error={traffic.error instanceof Error ? traffic.error.message : t("Unable to load connection snapshots.")} onRetry={() => void traffic.refetch()} /> : chartSeries.length ? (
            <div className="overview-chart-stack">
              <TrafficChart
                id="traffic-upload"
                label={t("Upload rate")}
                metric="tx_bps"
                series={chartSeries}
                formatValue={(value) => `${formatBytes(value)}/s`}
                formatDate={formatDate}
                locale={locale}
                t={t}
                tone="upload"
              />
              <TrafficChart
                id="traffic-download"
                label={t("Download rate")}
                metric="rx_bps"
                series={chartSeries}
                formatValue={(value) => `${formatBytes(value)}/s`}
                formatDate={formatDate}
                locale={locale}
                t={t}
                tone="download"
              />
              <TrafficChart
                id="traffic-connections"
                label={t("Active connections")}
                metric="active_connections"
                series={chartSeries}
                formatValue={formatNumber}
                formatDate={formatDate}
                locale={locale}
                t={t}
                tone="connections"
              />
            </div>
          ) : <Panel className="traffic-chart-state"><EmptyState title="No traffic samples" /></Panel>}
        </section>
      </section>
    </div>
  );
}

function OverviewMetric({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: ReactNode;
  detail: string;
  tone?: "danger";
}) {
  return <div className={`overview-metric${tone ? ` overview-metric-${tone}` : ""}`}><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>;
}

function buildSiteTrafficSeries(entries: ConnectionSnapshot[], siteId: string): TrafficSeriesPoint[] {
  const buckets = new Map<number, Map<string, ConnectionSnapshot>>();
  for (const entry of entries) {
    if (entry.site_id !== siteId) continue;
    const timestamp = new Date(entry.sampled_at).getTime();
    if (!Number.isFinite(timestamp)) continue;
    const bucketAt = Math.floor(timestamp / 60_000) * 60_000;
    const nodeSnapshots = buckets.get(bucketAt) || new Map<string, ConnectionSnapshot>();
    const previous = nodeSnapshots.get(entry.node_id);
    if (!previous || new Date(entry.sampled_at).getTime() > new Date(previous.sampled_at).getTime()) {
      nodeSnapshots.set(entry.node_id, entry);
    }
    buckets.set(bucketAt, nodeSnapshots);
  }
  return [...buckets.entries()]
    .sort(([left], [right]) => left - right)
    .map(([bucketAt, nodeSnapshots]) => {
      const totals = [...nodeSnapshots.values()].reduce(
        (current, entry) => ({
          tx_bps: current.tx_bps + (entry.tx_bps || 0),
          rx_bps: current.rx_bps + (entry.rx_bps || 0),
          active_connections: current.active_connections + (entry.active_connections || 0),
        }),
        { tx_bps: 0, rx_bps: 0, active_connections: 0 },
      );
      return { at: new Date(bucketAt).toISOString(), ...totals };
    });
}

function TrafficChart({
  id,
  label,
  metric,
  series,
  formatValue,
  formatDate,
  locale,
  t,
  tone,
}: {
  id: string;
  label: string;
  metric: TrafficMetricKey;
  series: TrafficSeriesPoint[];
  formatValue: (value: number) => string;
  formatDate: (value: string | null | undefined, withTime?: boolean) => string;
  locale: string;
  t: (key: string, values?: Record<string, string | number>) => string;
  tone: "upload" | "download" | "connections";
}) {
  const width = 1000;
  const height = 220;
  const plotLeft = 42;
  const plotRight = width - 10;
  const plotTop = 12;
  const plotBottom = height - 28;
  const plotWidth = plotRight - plotLeft;
  const plotHeight = plotBottom - plotTop;
  const values = series.map((point) => Math.max(0, point[metric]));
  const max = Math.max(0, ...values);
  const chartMax = max > 0 ? max * 1.12 : 1;
  const coordinateFor = (value: number, index: number) => {
    const x = series.length > 1 ? plotLeft + (index / (series.length - 1)) * plotWidth : plotLeft + plotWidth / 2;
    const y = plotBottom - (value / chartMax) * plotHeight;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  };
  const points = values.map(coordinateFor);
  const area = points.length ? `${plotLeft},${plotBottom} ${points.join(" ")} ${plotRight},${plotBottom}` : "";
  const latest = series[series.length - 1];
  const timeFormatter = new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hour12: false });
  const labelIndexes = [...new Set([0, Math.floor((series.length - 1) / 2), series.length - 1])];

  return (
    <Panel className={`traffic-chart-panel traffic-chart-${tone}`}>
      <div className="traffic-chart-header">
        <div>
          <span>{label}</span>
          <strong>{formatValue(latest?.[metric] || 0)}</strong>
          <small>{latest ? `${t("Current")} · ${t("Updated {date}", { date: formatDate(latest.at) })}` : t("No traffic samples")}</small>
        </div>
        <span className="traffic-chart-scale">{formatValue(max)}</span>
      </div>
      <div className="traffic-chart-plot">
        {points.length ? (
          <svg className="traffic-chart-svg" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={`${label} ${formatValue(latest?.[metric] || 0)}`}>
            <defs>
              <linearGradient id={`${id}-fill`} x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stopColor="currentColor" stopOpacity="0.28" />
                <stop offset="100%" stopColor="currentColor" stopOpacity="0.02" />
              </linearGradient>
            </defs>
            {[0, 0.333, 0.666, 1].map((ratio) => {
              const y = plotTop + ratio * plotHeight;
              const value = chartMax * (1 - ratio);
              return (
                <g key={ratio}>
                  <line x1={plotLeft} x2={plotRight} y1={y} y2={y} stroke="var(--gp-line)" strokeDasharray="2 4" />
                  <text x={plotLeft - 8} y={y + 4} textAnchor="end">{formatValue(value)}</text>
                </g>
              );
            })}
            <polygon fill={`url(#${id}-fill)`} points={area} />
            <polyline fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" points={points.join(" ")} />
            {labelIndexes.map((index) => {
              const point = series[index];
              if (!point) return null;
              const x = series.length > 1 ? plotLeft + (index / (series.length - 1)) * plotWidth : plotLeft + plotWidth / 2;
              return <text key={`${point.at}-${index}`} x={x} y={height - 8} textAnchor={index === 0 ? "start" : index === series.length - 1 ? "end" : "middle"}>{timeFormatter.format(new Date(point.at))}</text>;
            })}
          </svg>
        ) : <div className="traffic-chart-empty">{t("No traffic samples")}</div>}
      </div>
    </Panel>
  );
}

function TopologySiteDetail({
  site,
  nodes,
  state,
  formatNumber,
  formatBytes,
  t,
}: {
  site: Site;
  nodes: Node[];
  state: string;
  formatNumber: (value: number) => string;
  formatBytes: (value: number) => string;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  return (
    <article className="topology-site-detail">
      <header>
        <div>
          <span className="panel-kicker">{t("SELECTED SITE")}</span>
          <h3>{t(site.name)}</h3>
          <span className="mono">{site.slug}</span>
        </div>
        <div className="topology-site-detail-actions">
          <StatusBadge status={state} />
          <Link href="/blacklist">{t("Manage blacklist")} <ArrowRight size={14} /></Link>
        </div>
      </header>
      <div className="topology-site-facts">
        <div><span>{t("Nodes")}</span><strong>{formatNumber(nodes.length)}</strong></div>
        <div><span>{t("Online")}</span><strong>{formatNumber(nodes.filter((node) => node.liveness_status === "online").length)}</strong></div>
        <div><span>{t("In sync")}</span><strong>{formatNumber(nodes.filter((node) => node.config_status === "in_sync").length)}</strong></div>
      </div>
      {nodes.length ? (
        <div className="topology-node-list">
          {nodes.map((node) => (
            <div className="topology-node-row" key={node.id}>
              <div>
                <strong>{node.name}</strong>
                <small className="mono">{node.agent_id}</small>
                <small>{t("{count} connections", { count: node.active_connections || 0 })} · {t("↑ {value}", { value: `${formatBytes(node.tx_bps || 0)}/s` })} · {t("↓ {value}", { value: `${formatBytes(node.rx_bps || 0)}/s` })}</small>
              </div>
              <div>
                <StatusBadge status={node.liveness_status} />
                <StatusBadge status={node.config_status} />
              </div>
            </div>
          ))}
        </div>
      ) : <p className="topology-site-empty">{t("No node enrolled")}</p>}
    </article>
  );
}
