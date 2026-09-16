"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity } from "lucide-react";
import { useMemo, useState } from "react";
import { getConnections, getNodes, getSites, type ConnectionSnapshot } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { ListFilters, timeRangeStart, type TimeRange } from "../../components/list-filters";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Panel, RefreshButton } from "../../components/ui";

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
  const entries = connections.data || [];
  const latestByNode = new Map<string, ConnectionSnapshot>();
  for (const entry of entries) {
    if (!latestByNode.has(entry.node_id)) latestByNode.set(entry.node_id, entry);
  }
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
        description={t("Live sing-box sessions and recent connection summaries.")}
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
                <th>{t("Source IP")}</th>
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
                  <td className="mono">{entry.src_ip ? `${entry.src_ip}${entry.src_port ? `:${entry.src_port}` : ""}` : "-"}</td>
                  <td className="mono">{entry.dst_host || entry.dst_ip}{entry.dst_port ? `:${entry.dst_port}` : ""}</td>
                  <td>{[entry.inbound, entry.network].filter(Boolean).join(" · ") || "-"}</td>
                  <td>{entry.outbound_chain?.length ? entry.outbound_chain.join(" → ") : "-"}</td>
                  <td>
                    <span className="cell-secondary">{t("↓ {value}", { value: formatBytes(entry.bytes_down) })}</span>
                    <span className="cell-secondary">{t("↑ {value}", { value: formatBytes(entry.bytes_up) })}</span>
                  </td>
                  <td>{formatDate(entry.start || entry.sampled_at)}</td>
                </tr>
              )) : <tr><td colSpan={7}><EmptyState title="No live connections recorded." /></td></tr>}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}
