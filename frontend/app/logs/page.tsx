"use client";

import { useQuery } from "@tanstack/react-query";
import { FileWarning, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { getLogs, getNodes, getSites, type AccessLog } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { ListFilters, timeRangeStart, type TimeRange } from "../../components/list-filters";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { IconButton, Panel, StatusBadge } from "../../components/ui";

type ActionFilter = "" | "deny" | "allow";

export default function LogsPage() {
  const { t, formatDate, formatBytes, formatDuration, formatNumber } = usePreferences();
  const session = useManagementSession();
  const [action, setAction] = useState<ActionFilter>("deny");
  const [siteId, setSiteId] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [timeRange, setTimeRange] = useState<TimeRange>("24h");
  const [search, setSearch] = useState("");
  // The time range is an absolute request boundary, not a render-time value.
  const since = useMemo(() => timeRangeStart(timeRange), [timeRange]);
  const logs = useQuery({
    queryKey: ["logs", action, siteId, nodeId, since, search],
    queryFn: () => getLogs({ action: action || undefined, siteId: siteId || undefined, nodeId: nodeId || undefined, since, search: search.trim() || undefined }),
    enabled: session === true,
    refetchInterval: 10_000,
  });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, staleTime: 30_000 });

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (logs.isLoading || sites.isLoading || nodes.isLoading) return <LoadingState rows={7} />;
  if (logs.isError) return <ErrorState error={logs.error instanceof Error ? logs.error.message : "Unable to load access logs."} onRetry={() => void logs.refetch()} />;

  const siteNames = new Map((sites.data || []).map((site) => [site.id, site.name]));
  const nodeNames = new Map((nodes.data || []).map((node) => [node.agent_id, node.name]));
  const entries = logs.data || [];

  return (
    <div className="page-stack">
      <PageHeader eyebrow="OBSERVE" title="Access logs" description="Recent allow and deny decisions from edge monitors." actions={<IconButton label="Refresh" onClick={() => void logs.refetch()}><RefreshCw size={16} /></IconButton>} />
      <Panel>
        <div className="table-toolbar">
          <div className="segmented-control" role="group" aria-label={t("Action")}>
            {(["", "deny", "allow"] as ActionFilter[]).map((value) => <button key={value || "all"} className={action === value ? "segmented-active" : ""} onClick={() => setAction(value)}>{t(value === "" ? "All decisions" : value === "deny" ? "Only denies" : "Only allows")}</button>)}
          </div>
          <span className="toolbar-note">{t("{count} events", { count: formatNumber(entries.length) })}</span>
        </div>
        <ListFilters
          search={search}
          setSearch={setSearch}
          timeRange={timeRange}
          setTimeRange={setTimeRange}
          selects={[
            { label: "Site", value: siteId, setValue: (value) => { setSiteId(value); setNodeId(""); }, options: [{ value: "", label: "All sites" }, ...(sites.data || []).map((site) => ({ value: site.id, label: site.name }))] },
            { label: "Node", value: nodeId, setValue: setNodeId, options: [{ value: "", label: "All nodes" }, ...(nodes.data || []).filter((node) => !siteId || node.site_id === siteId).map((node) => ({ value: node.id, label: node.name }))] },
          ]}
        />
        <div className="table-wrap"><table><thead><tr><th>{t("Time")}</th><th>{t("Node")}</th><th>{t("Source")}</th><th>{t("Destination")}</th><th>{t("Action")}</th><th>{t("Deny reason")}</th><th>{t("Traffic")}</th><th>{t("Duration")}</th></tr></thead><tbody>{entries.length ? entries.map((entry) => <LogRow key={entry.id} entry={entry} siteName={siteNames.get(entry.site_id) || entry.site_id} nodeName={nodeNames.get(entry.node_id) || entry.node_id} formatDate={formatDate} formatBytes={formatBytes} formatDuration={formatDuration} formatNumber={formatNumber} t={t} />) : <tr><td colSpan={8}><EmptyState title="No access logs recorded." /></td></tr>}</tbody></table></div>
      </Panel>
    </div>
  );
}

function LogRow({ entry, siteName, nodeName, formatDate, formatBytes, formatDuration, formatNumber, t }: { entry: AccessLog; siteName: string; nodeName: string; formatDate: (value: string | null | undefined, withTime?: boolean) => string; formatBytes: (value: number | null | undefined) => string; formatDuration: (value: number | null | undefined) => string; formatNumber: (value: number | null | undefined, options?: Intl.NumberFormatOptions) => string; t: (key: string, values?: Record<string, string | number>) => string }) {
  return <tr><td>{formatDate(entry.ts)}</td><td><strong>{nodeName}</strong><span className="cell-secondary">{t(siteName)}</span></td><td className="mono">{entry.src_ip || "-"}</td><td><strong>{entry.dst_host || "-"}</strong><span className="cell-secondary">:{entry.dst_port ? formatNumber(entry.dst_port, { useGrouping: false }) : "-"}</span></td><td><StatusBadge status={entry.action === "deny" ? "denied" : "allowed"} /></td><td>{entry.deny_reason ? t(entry.deny_reason.replaceAll("_", " ")) : "-"}</td><td><span className="cell-secondary">{t("↑ {value}", { value: formatBytes(entry.bytes_up) })}</span><span className="cell-secondary">{t("↓ {value}", { value: formatBytes(entry.bytes_down) })}</span></td><td>{formatDuration(entry.duration_ms)}</td></tr>;
}
