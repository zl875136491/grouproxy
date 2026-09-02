"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity } from "lucide-react";
import { getConnections, getNodes, getSites } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { ListFilters, timeRangeStart, type TimeRange } from "../../components/list-filters";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Panel, RefreshButton, StatusBadge } from "../../components/ui";
import { useMemo, useState } from "react";

export default function ConnectionsPage() {
  const { t, formatDate, formatBytes, formatNumber } = usePreferences();
  const session = useManagementSession();
  const [siteId, setSiteId] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [timeRange, setTimeRange] = useState<TimeRange>("24h");
  const since = useMemo(() => timeRangeStart(timeRange), [timeRange]);
  const connections = useQuery({ queryKey: ["connections", siteId, nodeId, since], queryFn: () => getConnections({ siteId: siteId || undefined, nodeId: nodeId || undefined, since }), enabled: session === true, refetchInterval: 10_000 });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, staleTime: 30_000 });

  if (session === null) return <LoadingState rows={6} />;
  if (!session) return <SessionGate />;
  if (connections.isLoading || nodes.isLoading || sites.isLoading) return <LoadingState rows={6} />;
  if (connections.isError) return <ErrorState error={connections.error instanceof Error ? connections.error.message : "Unable to load connection snapshots."} onRetry={() => void connections.refetch()} />;

  const nodeNames = new Map((nodes.data || []).map((node) => [node.agent_id, node.name]));
  const siteNames = new Map((sites.data || []).map((site) => [site.id, site.name]));
  const entries = connections.data || [];
  return <div className="page-stack"><PageHeader eyebrow="OBSERVE" title="Connection snapshots" description="Latest connection summaries; full connection tables are never stored." actions={<RefreshButton label="Refresh" onRefresh={() => connections.refetch()} />} /><Panel><div className="table-toolbar"><div className="toolbar-title"><Activity size={18} /><span>{t("Connection history")}</span></div><span className="toolbar-note">{t("{count} snapshots", { count: formatNumber(entries.length) })}</span></div><ListFilters timeRange={timeRange} setTimeRange={setTimeRange} selects={[{ label: "Site", value: siteId, setValue: (value) => { setSiteId(value); setNodeId(""); }, options: [{ value: "", label: "All sites" }, ...(sites.data || []).map((site) => ({ value: site.id, label: site.name }))] }, { label: "Node", value: nodeId, setValue: setNodeId, options: [{ value: "", label: "All nodes" }, ...(nodes.data || []).filter((node) => !siteId || node.site_id === siteId).map((node) => ({ value: node.id, label: node.name }))] }]} /><div className="table-wrap"><table><thead><tr><th>{t("Sampled at")}</th><th>{t("Node")}</th><th>{t("Active connections")}</th><th>{t("Traffic")}</th><th>{t("API status")}</th><th>{t("Top destinations")}</th></tr></thead><tbody>{entries.length ? entries.map((entry) => <tr key={entry.id}><td>{formatDate(entry.sampled_at)}</td><td><strong>{nodeNames.get(entry.node_id) || entry.node_id}</strong><span className="cell-secondary">{t(siteNames.get(entry.site_id) || entry.site_id)}</span></td><td><span className="connection-count"><Activity size={15} />{formatNumber(entry.active_connections)}</span></td><td><span className="cell-secondary">{t("↑ {value}", { value: formatBytes(entry.bytes_up) })}</span><span className="cell-secondary">{t("↓ {value}", { value: formatBytes(entry.bytes_down) })}</span></td><td><StatusBadge status={entry.api_available ? "enabled" : "unhealthy"} /></td><td>{entry.top_destinations.length ? entry.top_destinations.slice(0, 3).map((item) => <span className="cell-secondary" key={item.label}>{item.label} · {formatNumber(item.connections)}</span>) : "-"}</td></tr>) : <tr><td colSpan={6}><EmptyState title="No connection snapshots recorded." /></td></tr>}</tbody></table></div></Panel></div>;
}
