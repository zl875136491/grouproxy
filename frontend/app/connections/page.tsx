"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity } from "lucide-react";
import { useMemo, useState } from "react";
import { getConnections, getNodes, getSites, type ConnectionSnapshot, type Node } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { ListFilters, timeRangeStart, type TimeRange } from "../../components/list-filters";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Panel, RefreshButton, StatusBadge } from "../../components/ui";

export default function ConnectionsPage() {
  const { t, formatDate, formatBytes, formatNumber } = usePreferences();
  const session = useManagementSession();
  const [siteId, setSiteId] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [timeRange, setTimeRange] = useState<TimeRange>("24h");
  const since = useMemo(() => timeRangeStart(timeRange), [timeRange]);
  const connections = useQuery({
    queryKey: ["connections", siteId, nodeId, since],
    queryFn: () => getConnections({
      siteId: siteId || undefined,
      nodeId: nodeId || undefined,
      since,
      limit: 250,
    }),
    enabled: session === true,
    refetchInterval: 10_000,
  });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, staleTime: 30_000 });

  if (session === null) return <LoadingState rows={6} />;
  if (!session) return <SessionGate />;
  if (connections.isLoading || nodes.isLoading || sites.isLoading) return <LoadingState rows={6} />;
  if (connections.isError) return <ErrorState error={connections.error instanceof Error ? connections.error.message : t("Unable to load connection snapshots.")} onRetry={() => void connections.refetch()} />;

  const nodeItems = nodes.data || [];
  const siteNames = new Map((sites.data || []).map((site) => [site.id, site.name]));
  const entries = connections.data || [];
  const snapshotsByNode = new Map<string, ConnectionSnapshot[]>();
  for (const entry of [...entries].reverse()) {
    const current = snapshotsByNode.get(entry.node_id) || [];
    current.push(entry);
    snapshotsByNode.set(entry.node_id, current);
  }
  const latestByNode = new Map<string, ConnectionSnapshot>();
  for (const [agentId, snapshots] of snapshotsByNode) {
    latestByNode.set(agentId, snapshots[snapshots.length - 1]);
  }
  const overviewNodes = nodeItems.filter((node) => (
    (!siteId || node.site_id === siteId)
    && (!nodeId || node.id === nodeId || node.agent_id === nodeId)
  ));
  const liveRows = [...latestByNode.values()].flatMap((entry) => (entry.connections || []).map((item) => ({
    ...item,
    node_id: entry.node_id,
    sampled_at: entry.sampled_at,
  })));
  const nodeNames = new Map(nodeItems.map((node) => [node.agent_id, node.name]));

  return (
    <div className="page-stack page-fill list-page connections-page">
      <PageHeader
        eyebrow="OBSERVE"
        title={t("Connection snapshots")}
        description={t("Live sing-box sessions and per-node traffic overview.")}
        actions={<RefreshButton label={t("Refresh")} onRefresh={() => connections.refetch()} />}
      />
      <Panel className="list-panel">
        <div className="table-toolbar">
          <div className="toolbar-title"><Activity size={18} /><span>{t("Live connections")}</span></div>
          <span className="toolbar-note">{t("{count} sessions", { count: formatNumber(liveRows.length) })}</span>
        </div>
        <ListFilters
          timeRange={timeRange}
          setTimeRange={setTimeRange}
          selects={[
            { label: "Site", value: siteId, setValue: (value) => { setSiteId(value); setNodeId(""); }, options: [{ value: "", label: "All sites" }, ...(sites.data || []).map((site) => ({ value: site.id, label: site.name }))] },
            { label: "Node", value: nodeId, setValue: setNodeId, options: [{ value: "", label: "All nodes" }, ...nodeItems.filter((node) => !siteId || node.site_id === siteId).map((node) => ({ value: node.id, label: node.name }))] },
          ]}
        />
        <div className="table-wrap table-scroll">
          <table>
            <thead>
              <tr>
                <th>{t("Node")}</th>
                <th>{t("Destination")}</th>
                <th>{t("Protocol")}</th>
                <th>{t("Outbound")}</th>
                <th>{t("Traffic")}</th>
                <th>{t("Started")}</th>
              </tr>
            </thead>
            <tbody>
              {liveRows.length ? liveRows.map((entry, index) => (
                <tr key={entry.id || `${entry.node_id}-${index}`}>
                  <td><strong>{nodeNames.get(entry.node_id) || entry.node_id}</strong></td>
                  <td className="mono">{entry.dst_host || entry.dst_ip}{entry.dst_port ? `:${entry.dst_port}` : ""}</td>
                  <td>{[entry.inbound, entry.network].filter(Boolean).join(" · ") || "-"}</td>
                  <td>{entry.outbound_chain?.length ? entry.outbound_chain.join(" → ") : "-"}</td>
                  <td>
                    <span className="cell-secondary">{t("↓ {value}", { value: formatBytes(entry.bytes_down) })}</span>
                    <span className="cell-secondary">{t("↑ {value}", { value: formatBytes(entry.bytes_up) })}</span>
                  </td>
                  <td>{formatDate(entry.start || entry.sampled_at)}</td>
                </tr>
              )) : <tr><td colSpan={6}><EmptyState title="No live connections recorded." /></td></tr>}
            </tbody>
          </table>
        </div>
      </Panel>
      <section className="traffic-overview" aria-label={t("Traffic overview")}>
        <div className="table-toolbar traffic-overview-heading">
          <div className="toolbar-title"><Activity size={18} /><span>{t("Traffic overview")}</span></div>
          <span className="toolbar-note">{t("{count} nodes", { count: formatNumber(overviewNodes.length) })}</span>
        </div>
        {overviewNodes.length ? (
          <div className="traffic-overview-grid">
            {overviewNodes.map((node) => (
              <NodeTrafficCard
                key={node.id}
                node={node}
                siteName={t(siteNames.get(node.site_id) || node.site_id)}
                snapshots={snapshotsByNode.get(node.agent_id) || []}
              />
            ))}
          </div>
        ) : <EmptyState title="No node enrolled" />}
      </section>
    </div>
  );
}

function NodeTrafficCard({
  node,
  siteName,
  snapshots,
}: {
  node: Node;
  siteName: string;
  snapshots: ConnectionSnapshot[];
}) {
  const { t, formatBytes, formatNumber, formatDate } = usePreferences();
  const latest = snapshots[snapshots.length - 1];
  const txValues = snapshots.map((item) => item.tx_bps || 0);
  const rxValues = snapshots.map((item) => item.rx_bps || 0);
  const connectionValues = snapshots.map((item) => item.active_connections || 0);
  const txNow = latest?.tx_bps ?? node.tx_bps ?? 0;
  const rxNow = latest?.rx_bps ?? node.rx_bps ?? 0;
  const connectionsNow = latest?.active_connections ?? node.active_connections ?? 0;
  const bytesUp = latest?.bytes_up ?? node.bytes_up ?? 0;
  const bytesDown = latest?.bytes_down ?? node.bytes_down ?? 0;
  return (
    <Panel className="traffic-node-card">
      <div className="panel-heading">
        <div>
          <span className="panel-kicker">{siteName}</span>
          <h2>{node.name}</h2>
          <p className="mono">{node.agent_id}</p>
        </div>
        <StatusBadge status={node.liveness_status} />
      </div>
      <div className="traffic-metric-grid">
        <TrafficMetric
          label={t("Upload rate")}
          value={`${formatBytes(txNow)}/s`}
          detail={t("Total {value}", { value: formatBytes(bytesUp) })}
          values={txValues}
          formatValue={(value) => `${formatBytes(value)}/s`}
        />
        <TrafficMetric
          label={t("Download rate")}
          value={`${formatBytes(rxNow)}/s`}
          detail={t("Total {value}", { value: formatBytes(bytesDown) })}
          values={rxValues}
          formatValue={(value) => `${formatBytes(value)}/s`}
        />
        <TrafficMetric
          className="traffic-metric-wide"
          label={t("Active connections")}
          value={formatNumber(connectionsNow)}
          detail={latest ? t("Updated {date}", { date: formatDate(latest.sampled_at) }) : t("No traffic samples")}
          values={connectionValues}
          formatValue={(value) => formatNumber(value)}
        />
      </div>
    </Panel>
  );
}

function TrafficMetric({
  label,
  value,
  detail,
  values,
  formatValue,
  className,
}: {
  label: string;
  value: string;
  detail: string;
  values: number[];
  formatValue: (value: number) => string;
  className?: string;
}) {
  return (
    <div className={`traffic-metric ${className || ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <TrafficSparkline values={values} formatValue={formatValue} />
      <small>{detail}</small>
    </div>
  );
}

function TrafficSparkline({
  values,
  formatValue,
}: {
  values: number[];
  formatValue: (value: number) => string;
}) {
  const width = 240;
  const height = 56;
  const max = Math.max(0, ...values);
  const points = values.length > 1
    ? values.map((value, index) => {
      const x = (index / (values.length - 1)) * width;
      const y = height - 4 - ((max > 0 ? value / max : 0) * (height - 8));
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    : [];
  const area = points.length ? `0,${height} ${points.join(" ")} ${width},${height}` : "";
  return (
    <div className="traffic-sparkline-wrap">
      <svg className="traffic-sparkline" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
        {points.length ? (
          <>
            <polygon fill="currentColor" opacity="0.14" points={area} />
            <polyline fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinejoin="round" strokeLinecap="round" points={points.join(" ")} />
          </>
        ) : (
          <line x1="0" y1={height - 4} x2={width} y2={height - 4} stroke="currentColor" strokeOpacity="0.28" />
        )}
      </svg>
      <span className="traffic-sparkline-scale">{max > 0 ? formatValue(max) : ""}</span>
    </div>
  );
}
