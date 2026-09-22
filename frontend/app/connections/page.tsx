"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, ChevronLeft, ChevronRight, History, Search } from "lucide-react";
import { useMemo, useState, type SetStateAction } from "react";
import {
  getConnectionHistory,
  getLiveConnections,
  getNodes,
  getSites,
  type ConnectionLiveItem,
  type ConnectionSnapshot,
} from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { FilterSelect, ListFilters, timeRangeStart, type TimeRange } from "../../components/list-filters";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, Panel, RefreshButton } from "../../components/ui";

type ConnectionView = "live" | "history";

type LiveRow = ConnectionLiveItem & {
  node_id: string;
  sampled_at: string;
};

type HistoryRow = {
  snapshot: ConnectionSnapshot;
  connection: ConnectionLiveItem | null;
  key: string;
};

function destinationLabel(connection: ConnectionLiveItem | null): string {
  if (!connection) return "";
  const host = connection.dst_host || connection.dst_ip;
  return `${host}${connection.dst_port ? `:${connection.dst_port}` : ""}`;
}

function sourceLabel(connection: ConnectionLiveItem | null): string {
  if (!connection) return "";
  return `${connection.src_ip}${connection.src_port ? `:${connection.src_port}` : ""}`;
}

function liveRowsFromSnapshots(snapshots: ConnectionSnapshot[]): LiveRow[] {
  return snapshots.flatMap((snapshot) => (snapshot.connections || []).map((connection) => ({
    ...connection,
    node_id: snapshot.node_id,
    sampled_at: snapshot.sampled_at,
  })));
}

function historyRowsFromSnapshots(snapshots: ConnectionSnapshot[]): HistoryRow[] {
  const rows: HistoryRow[] = [];
  for (const snapshot of snapshots) {
    const connections = snapshot.connections || [];
    if (!connections.length) {
      rows.push({ snapshot, connection: null, key: `${snapshot.id}:summary` });
      continue;
    }
    connections.forEach((connection, index) => rows.push({
      snapshot,
      connection,
      key: `${snapshot.id}:${connection.id || index}`,
    }));
  }
  return rows;
}

function ConnectionTextFilter({
  label,
  placeholder,
  value,
  setValue,
}: {
  label: string;
  placeholder: string;
  value: string;
  setValue: (value: string) => void;
}) {
  const { t } = usePreferences();
  return (
    <label className="filter-search connection-text-filter">
      <Search size={15} aria-hidden="true" />
      <span className="sr-only">{t(label)}</span>
      <input
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder={t(placeholder)}
        aria-label={t(label)}
      />
    </label>
  );
}

export default function ConnectionsPage() {
  const { t, formatDate, formatBytes, formatNumber } = usePreferences();
  const session = useManagementSession();
  const [viewMode, setViewMode] = useState<ConnectionView>("live");
  const [siteId, setSiteId] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [timeRange, setTimeRange] = useState<TimeRange>("24h");
  const [network, setNetwork] = useState("");
  const [search, setSearch] = useState("");
  const [sourceIp, setSourceIp] = useState("");
  const [destination, setDestination] = useState("");
  const [outbound, setOutbound] = useState("");
  const [historyOffset, setHistoryOffset] = useState(0);
  const since = useMemo(() => timeRangeStart(timeRange), [timeRange]);

  const sites = useQuery({
    queryKey: ["sites"],
    queryFn: getSites,
    enabled: session === true,
    staleTime: 30_000,
  });
  const nodes = useQuery({
    queryKey: ["nodes"],
    queryFn: getNodes,
    enabled: session === true,
    staleTime: 10_000,
  });
  const live = useQuery({
    queryKey: ["connections-live", siteId, nodeId],
    queryFn: () => getLiveConnections({ siteId: siteId || undefined, nodeId: nodeId || undefined }),
    enabled: session === true && viewMode === "live",
    refetchInterval: 10_000,
  });
  const history = useQuery({
    queryKey: [
      "connections-history",
      siteId,
      nodeId,
      since,
      network,
      search,
      sourceIp,
      destination,
      outbound,
      historyOffset,
    ],
    queryFn: () => getConnectionHistory({
      siteId: siteId || undefined,
      nodeId: nodeId || undefined,
      since,
      network: network || undefined,
      search: search.trim() || undefined,
      sourceIp: sourceIp.trim() || undefined,
      destination: destination.trim() || undefined,
      outbound: outbound.trim() || undefined,
      limit: 100,
      offset: historyOffset,
    }),
    enabled: session === true && viewMode === "history",
    refetchInterval: 30_000,
  });

  if (session === null) return <LoadingState rows={6} />;
  if (!session) return <SessionGate />;
  if (sites.isLoading || nodes.isLoading) return <LoadingState rows={6} />;
  if (sites.isError || nodes.isError) {
    const issue = sites.error || nodes.error;
    return <ErrorState error={issue instanceof Error ? issue.message : t("The control plane did not respond.")} onRetry={() => void Promise.all([sites.refetch(), nodes.refetch()])} />;
  }
  if (viewMode === "live" && live.isError) {
    return <ErrorState error={live.error instanceof Error ? live.error.message : t("Unable to load live connections.")} onRetry={() => void live.refetch()} />;
  }
  if (viewMode === "history" && history.isError) {
    return <ErrorState error={history.error instanceof Error ? history.error.message : t("Unable to load connection history.")} onRetry={() => void history.refetch()} />;
  }

  const nodeItems = nodes.data || [];
  const nodeNames = new Map(nodeItems.map((node) => [node.agent_id, node.name]));
  const siteOptions = [{ value: "", label: "All sites" }, ...(sites.data || []).map((site) => ({ value: site.id, label: site.name }))];
  const nodeOptions = [
    { value: "", label: "All nodes" },
    ...nodeItems.filter((node) => !siteId || node.site_id === siteId).map((node) => ({ value: node.id, label: node.name })),
  ];
  const liveRows = liveRowsFromSnapshots(live.data || []);
  const historyRows = historyRowsFromSnapshots(history.data?.items || []);
  const historyTotal = history.data?.total || 0;

  function changeSite(value: string) {
    setSiteId(value);
    setNodeId("");
    setHistoryOffset(0);
  }

  function changeNode(value: string) {
    setNodeId(value);
    setHistoryOffset(0);
  }

  function changeTimeRange(value: SetStateAction<TimeRange>) {
    setTimeRange(value);
    setHistoryOffset(0);
  }

  return (
    <div className="page-stack page-fill list-page connections-page">
      <PageHeader
        eyebrow="OBSERVE"
        title={t("Connections")}
        description={t("Live connection information and retained history for audit.")}
        actions={<RefreshButton label={t("Refresh")} onRefresh={() => viewMode === "live" ? live.refetch() : history.refetch()} />}
      />
      <Panel className="list-panel connections-panel">
        <div className="table-toolbar connections-toolbar">
          <div>
            <div className="toolbar-title">
              {viewMode === "live" ? <Activity size={18} /> : <History size={18} />}
              <span>{t(viewMode === "live" ? "Live connection information" : "Historical connection records")}</span>
            </div>
            <span className="toolbar-note">
              {viewMode === "live"
                ? t("{count} sessions", { count: formatNumber(liveRows.length) })
                : t("{count} snapshots · {rows} observed rows", { count: formatNumber(historyTotal), rows: formatNumber(historyRows.length) })}
            </span>
          </div>
          <div className="segmented-control" role="group" aria-label={t("Connection view") }>
            <button type="button" className={viewMode === "live" ? "segmented-active" : ""} aria-pressed={viewMode === "live"} onClick={() => setViewMode("live")}>
              <Activity size={15} />{t("Live")}
            </button>
            <button type="button" className={viewMode === "history" ? "segmented-active" : ""} aria-pressed={viewMode === "history"} onClick={() => setViewMode("history")}>
              <History size={15} />{t("History")}
            </button>
          </div>
        </div>

        {viewMode === "live" ? (
          <div className="list-filters connections-live-filters" role="search">
            <FilterSelect label="Site" value={siteId} setValue={changeSite} options={siteOptions} />
            <FilterSelect label="Node" value={nodeId} setValue={changeNode} options={nodeOptions} />
          </div>
        ) : (
          <>
            <ListFilters
              search={search}
              setSearch={(value) => { setSearch(value); setHistoryOffset(0); }}
              searchPlaceholder="Search connection history"
              timeRange={timeRange}
              setTimeRange={changeTimeRange}
              selects={[
                { label: "Site", value: siteId, setValue: changeSite, options: siteOptions },
                { label: "Node", value: nodeId, setValue: changeNode, options: nodeOptions },
                { label: "Protocol", value: network, setValue: (value) => { setNetwork(value); setHistoryOffset(0); }, options: [{ value: "", label: "All protocols" }, { value: "tcp", label: "TCP" }, { value: "udp", label: "UDP" }] },
              ]}
            />
            <div className="connection-history-filter-row">
              <ConnectionTextFilter label="Source IP" placeholder="Source IP" value={sourceIp} setValue={(value) => { setSourceIp(value); setHistoryOffset(0); }} />
              <ConnectionTextFilter label="Destination" placeholder="Destination" value={destination} setValue={(value) => { setDestination(value); setHistoryOffset(0); }} />
              <ConnectionTextFilter label="Outbound" placeholder="Outbound" value={outbound} setValue={(value) => { setOutbound(value); setHistoryOffset(0); }} />
            </div>
          </>
        )}

        {viewMode === "live" ? (
          <LiveConnectionsTable rows={liveRows} nodeNames={nodeNames} formatDate={formatDate} formatBytes={formatBytes} t={t} />
        ) : (
          <HistoryConnectionsTable rows={historyRows} nodeNames={nodeNames} formatDate={formatDate} formatBytes={formatBytes} formatNumber={formatNumber} t={t} />
        )}

        {viewMode === "history" ? (
          <div className="connection-history-pagination">
            <span className="toolbar-note">{t("Showing {from}-{to} of {count} snapshots", { from: historyTotal ? historyOffset + 1 : 0, to: Math.min(historyOffset + (history.data?.items.length || 0), historyTotal), count: formatNumber(historyTotal) })}</span>
            <div className="row-actions">
              <Button size="sm" disabled={historyOffset === 0 || history.isFetching} onClick={() => setHistoryOffset(Math.max(0, historyOffset - 100))}>
                <ChevronLeft size={15} />{t("Previous")}
              </Button>
              <Button size="sm" disabled={!history.data?.has_more || history.isFetching} onClick={() => setHistoryOffset(historyOffset + 100)}>
                {t("Next")}<ChevronRight size={15} />
              </Button>
            </div>
          </div>
        ) : null}
      </Panel>
    </div>
  );
}

function LiveConnectionsTable({
  rows,
  nodeNames,
  formatDate,
  formatBytes,
  t,
}: {
  rows: LiveRow[];
  nodeNames: Map<string, string>;
  formatDate: (value: string | null | undefined, withTime?: boolean) => string;
  formatBytes: (value: number | null | undefined) => string;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  return (
    <div className="table-wrap table-scroll connection-table-wrap">
      <table>
        <thead><tr>
          <th>{t("Node")}</th><th>{t("Source IP")}</th><th>{t("Destination")}</th><th>{t("Protocol")}</th><th>{t("Outbound")}</th><th>{t("Traffic")}</th><th>{t("Started")}</th>
        </tr></thead>
        <tbody>
          {rows.length ? rows.map((entry, index) => (
            <tr key={entry.id || `${entry.node_id}-${index}`}>
              <td><strong>{nodeNames.get(entry.node_id) || entry.node_id}</strong><span className="cell-secondary">{t("Sampled at")}: {formatDate(entry.sampled_at)}</span></td>
              <td className="mono">{sourceLabel(entry) || "-"}</td>
              <td className="mono">{destinationLabel(entry) || "-"}</td>
              <td>{[entry.inbound, entry.network].filter(Boolean).join(" · ") || "-"}</td>
              <td>{entry.outbound_chain?.length ? entry.outbound_chain.join(" → ") : "-"}</td>
              <td><span className="cell-secondary">{t("↓ {value}", { value: formatBytes(entry.bytes_down) })}</span><span className="cell-secondary">{t("↑ {value}", { value: formatBytes(entry.bytes_up) })}</span></td>
              <td>{formatDate(entry.start || entry.sampled_at)}</td>
            </tr>
          )) : <tr><td colSpan={7}><EmptyState title="No live connections recorded." /></td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function HistoryConnectionsTable({
  rows,
  nodeNames,
  formatDate,
  formatBytes,
  formatNumber,
  t,
}: {
  rows: HistoryRow[];
  nodeNames: Map<string, string>;
  formatDate: (value: string | null | undefined, withTime?: boolean) => string;
  formatBytes: (value: number | null | undefined) => string;
  formatNumber: (value: number | null | undefined) => string;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  return (
    <div className="table-wrap table-scroll connection-table-wrap">
      <table>
        <thead><tr>
          <th>{t("Node")}</th><th>{t("Sampled at")}</th><th>{t("Source IP")}</th><th>{t("Destination")}</th><th>{t("Protocol")}</th><th>{t("Outbound")}</th><th>{t("Traffic")}</th><th>{t("Active connections")}</th>
        </tr></thead>
        <tbody>
          {rows.length ? rows.map((row) => {
            const { snapshot, connection } = row;
            const bytesUp = connection ? connection.bytes_up : snapshot.bytes_up;
            const bytesDown = connection ? connection.bytes_down : snapshot.bytes_down;
            return (
              <tr key={row.key}>
                <td><strong>{nodeNames.get(snapshot.node_id) || snapshot.node_id}</strong><span className="cell-secondary">{snapshot.api_available ? t("Available") : t("Unavailable")}</span></td>
                <td>{formatDate(snapshot.sampled_at)}</td>
                <td className="mono">{sourceLabel(connection) || t("Snapshot summary")}</td>
                <td className="mono">{destinationLabel(connection) || "-"}</td>
                <td>{connection ? [connection.inbound, connection.network].filter(Boolean).join(" · ") || "-" : "-"}</td>
                <td>{connection?.outbound_chain?.length ? connection.outbound_chain.join(" → ") : "-"}</td>
                <td><span className="cell-secondary">{t("↓ {value}", { value: formatBytes(bytesDown) })}</span><span className="cell-secondary">{t("↑ {value}", { value: formatBytes(bytesUp) })}</span></td>
                <td>{formatNumber(snapshot.active_connections)}</td>
              </tr>
            );
          }) : <tr><td colSpan={8}><EmptyState title="No connection history recorded." /></td></tr>}
        </tbody>
      </table>
    </div>
  );
}
