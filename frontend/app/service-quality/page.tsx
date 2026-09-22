"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowDownUp, BarChart3, CalendarRange, List, Table2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  getNodes,
  getServiceQuality,
  getSites,
  type ServiceQualityStat,
  type ServiceQualityWindow,
} from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { FilterSelect } from "../../components/list-filters";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Panel, RefreshButton, StatusBadge } from "../../components/ui";

type ViewMode = "chart" | "table";
type SortKey = "node_name" | "outbound_tag" | "average_latency_ms" | "success_rate" | "sample_count" | "last_sampled_at";

const qualityWindows: Array<{ value: ServiceQualityWindow; label: string }> = [
  { value: "1h", label: "Last hour" },
  { value: "24h", label: "Last 24 hours" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
];

function compareValues(left: ServiceQualityStat, right: ServiceQualityStat, key: SortKey) {
  const leftValue = left[key];
  const rightValue = right[key];
  if (leftValue === rightValue) return 0;
  if (leftValue === null || leftValue === undefined) return 1;
  if (rightValue === null || rightValue === undefined) return -1;
  if (typeof leftValue === "string" && typeof rightValue === "string") {
    return leftValue.localeCompare(rightValue);
  }
  return Number(leftValue) - Number(rightValue);
}

function qualityStatus(entry: ServiceQualityStat): string {
  if (entry.sample_count === 0 || entry.last_success === null) return "pending";
  if (!entry.last_success) return "offline";
  if (entry.average_latency_ms !== null && entry.average_latency_ms > 800) return "attention";
  if (entry.average_latency_ms !== null && entry.average_latency_ms > 250) return "degraded";
  return "healthy";
}

export default function ServiceQualityPage() {
  const { t, formatDate, formatDuration, formatNumber, formatPercent } = usePreferences();
  const session = useManagementSession();
  const [siteId, setSiteId] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [window, setWindow] = useState<ServiceQualityWindow>("24h");
  const [viewMode, setViewMode] = useState<ViewMode>("chart");
  const [sortKey, setSortKey] = useState<SortKey>("average_latency_ms");
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc");
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, staleTime: 10_000 });
  const quality = useQuery({
    queryKey: ["service-quality", window, siteId, nodeId],
    queryFn: () => getServiceQuality({ window, siteId: siteId || undefined, nodeId: nodeId || undefined }),
    enabled: session === true,
    refetchInterval: 30_000,
  });

  const nodeItems = useMemo(
    () => (nodes.data || []).filter((node) => !siteId || node.site_id === siteId),
    [nodes.data, siteId],
  );
  useEffect(() => {
    if (nodeId && !nodeItems.some((node) => node.id === nodeId)) setNodeId("");
  }, [nodeId, nodeItems]);

  const entries = useMemo(() => {
    const rows = [...(quality.data?.entries || [])];
    return rows.sort((left, right) => {
      const result = compareValues(left, right, sortKey);
      return result === 0 ? left.outbound_tag.localeCompare(right.outbound_tag) : sortDirection === "asc" ? result : -result;
    });
  }, [quality.data?.entries, sortDirection, sortKey]);

  if (session === null) return <LoadingState rows={8} />;
  if (!session) return <SessionGate />;
  if (sites.isLoading || nodes.isLoading) return <LoadingState rows={8} />;
  if (sites.isError || nodes.isError) {
    const issue = sites.error || nodes.error;
    return <ErrorState error={issue instanceof Error ? issue.message : t("The control plane did not respond.")} onRetry={() => void Promise.all([sites.refetch(), nodes.refetch()])} />;
  }

  function selectSort(nextKey: SortKey) {
    if (sortKey === nextKey) {
      setSortDirection((current) => current === "asc" ? "desc" : "asc");
    } else {
      setSortKey(nextKey);
      setSortDirection(nextKey === "average_latency_ms" || nextKey === "success_rate" ? "asc" : "asc");
    }
  }

  return (
    <div className="page-stack service-quality-page">
      <PageHeader
        eyebrow="OBSERVE"
        title={t("Service quality")}
        description={t("Average latency and availability for every subscription outbound.")}
        icon={<BarChart3 size={18} />}
        actions={<RefreshButton label={t("Refresh")} onRefresh={() => quality.refetch()} />}
      />
      <Panel className="service-quality-toolbar-panel">
        <div className="table-toolbar service-quality-toolbar">
          <div>
            <div className="toolbar-title"><BarChart3 size={18} /><span>{t("Subscription outbound performance")}</span></div>
            <span className="toolbar-note">{t("{count} services", { count: formatNumber(entries.length) })} · {t("{count} samples", { count: formatNumber(quality.data?.samples || 0) })}</span>
          </div>
          <div className="segmented-control" role="group" aria-label={t("View mode")}>
            <button
              type="button"
              className={viewMode === "chart" ? "segmented-active" : ""}
              aria-pressed={viewMode === "chart"}
              onClick={() => setViewMode("chart")}
            >
              <BarChart3 size={15} />{t("Chart")}
            </button>
            <button
              type="button"
              className={viewMode === "table" ? "segmented-active" : ""}
              aria-pressed={viewMode === "table"}
              onClick={() => setViewMode("table")}
            >
              <Table2 size={15} />{t("Sortable list")}
            </button>
          </div>
        </div>
        <div className="list-filters service-quality-filters" role="search">
          <FilterSelect
            label="Site"
            value={siteId}
            setValue={(value) => { setSiteId(value); setNodeId(""); }}
            options={[{ value: "", label: "All sites" }, ...(sites.data || []).map((site) => ({ value: site.id, label: site.name }))]}
          />
          <FilterSelect
            label="Node"
            value={nodeId}
            setValue={setNodeId}
            options={[{ value: "", label: "All nodes" }, ...nodeItems.map((node) => ({ value: node.id, label: node.name }))]}
          />
          <FilterSelect label="Time range" value={window} setValue={(value) => setWindow(value as ServiceQualityWindow)} options={qualityWindows} icon={<CalendarRange size={15} aria-hidden="true" />} />
        </div>
      </Panel>
      {quality.isLoading ? <Panel><LoadingState rows={8} /></Panel> : quality.isError ? <ErrorState error={quality.error instanceof Error ? quality.error.message : t("Unable to load service quality.")} onRetry={() => void quality.refetch()} /> : entries.length === 0 ? <Panel><EmptyState title="No service quality samples." detail="The monitor will report subscription outbound latency after its next proxy snapshot." /></Panel> : viewMode === "chart" ? <QualityChart entries={entries} formatDuration={formatDuration} formatNumber={formatNumber} t={t} /> : <QualityTable entries={entries} formatDate={formatDate} formatDuration={formatDuration} formatNumber={formatNumber} formatPercent={formatPercent} selectSort={selectSort} sortDirection={sortDirection} sortKey={sortKey} t={t} />}
    </div>
  );
}

function QualityChart({
  entries,
  formatDuration,
  formatNumber,
  t,
}: {
  entries: ServiceQualityStat[];
  formatDuration: (value: number | null | undefined) => string;
  formatNumber: (value: number | null | undefined) => string;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  const measured = entries.map((entry) => entry.average_latency_ms).filter((value): value is number => value !== null);
  const max = Math.max(1, ...measured);
  return (
    <Panel className="service-quality-chart-panel">
      <div className="panel-heading">
        <div><span className="panel-kicker">{t("AVERAGE LATENCY")}</span><h2>{t("Subscription: outbound comparison")}</h2></div>
        <span className="toolbar-note">{t("Lower is better")}</span>
      </div>
      <div className="quality-chart" role="img" aria-label={t("Average latency by node and subscription outbound")}>
        <div className="quality-chart-axis"><span>{formatDuration(0)}</span><span>{formatDuration(max)}</span></div>
        {entries.map((entry) => {
          const value = entry.average_latency_ms;
          const percentage = value === null ? 0 : Math.max(2, (value / max) * 100);
          return (
            <div className="quality-chart-row" key={`${entry.node_id}:${entry.outbound_tag}`}>
              <div className="quality-chart-label">
                <strong>{entry.node_name}</strong>
                <span>{t("Subscription")} · {entry.outbound_tag}</span>
              </div>
              <div className="quality-chart-track" role="progressbar" aria-label={`${entry.node_name} ${entry.outbound_tag}`} aria-valuemin={0} aria-valuemax={max} aria-valuenow={value || 0}>
                {value !== null ? <span className="quality-chart-bar" style={{ width: `${Math.min(100, percentage)}%` }} /> : null}
              </div>
              <strong className="quality-chart-value">{value === null ? t("No samples") : formatDuration(value)}</strong>
            </div>
          );
        })}
      </div>
      <p className="quality-chart-note">{t("{count} samples", { count: formatNumber(entries.reduce((total, entry) => total + entry.sample_count, 0)) })} · {t("Availability is calculated from monitor reports.")}</p>
    </Panel>
  );
}

function QualityTable({
  entries,
  formatDate,
  formatDuration,
  formatNumber,
  formatPercent,
  selectSort,
  sortDirection,
  sortKey,
  t,
}: {
  entries: ServiceQualityStat[];
  formatDate: (value: string | null | undefined, withTime?: boolean) => string;
  formatDuration: (value: number | null | undefined) => string;
  formatNumber: (value: number | null | undefined, options?: Intl.NumberFormatOptions) => string;
  formatPercent: (value: number | null | undefined) => string;
  selectSort: (key: SortKey) => void;
  sortDirection: "asc" | "desc";
  sortKey: SortKey;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  return (
    <Panel className="service-quality-table-panel">
      <div className="table-toolbar"><div className="toolbar-title"><List size={18} /><span>{t("Sortable quality list")}</span></div><span className="toolbar-note">{t("Select a column to sort")}</span></div>
      <div className="table-wrap table-scroll service-quality-table-wrap">
        <table>
          <thead><tr>
            <SortableHeader label="Node" sortKey="node_name" activeKey={sortKey} direction={sortDirection} onSort={selectSort} t={t} />
            <SortableHeader label="Subscription outbound" sortKey="outbound_tag" activeKey={sortKey} direction={sortDirection} onSort={selectSort} t={t} />
            <SortableHeader label="Average latency" sortKey="average_latency_ms" activeKey={sortKey} direction={sortDirection} onSort={selectSort} t={t} />
            <SortableHeader label="Availability" sortKey="success_rate" activeKey={sortKey} direction={sortDirection} onSort={selectSort} t={t} />
            <th>{t("Range")}</th>
            <SortableHeader label="Samples" sortKey="sample_count" activeKey={sortKey} direction={sortDirection} onSort={selectSort} t={t} />
            <SortableHeader label="Last sample" sortKey="last_sampled_at" activeKey={sortKey} direction={sortDirection} onSort={selectSort} t={t} />
          </tr></thead>
          <tbody>{entries.map((entry) => <tr key={`${entry.node_id}:${entry.outbound_tag}`}>
            <td><strong>{entry.node_name}</strong><span className="cell-secondary">{entry.site_name}</span></td>
            <td><strong>{t("Subscription")}</strong><span className="cell-secondary mono">{entry.outbound_tag}</span></td>
            <td><strong>{formatDuration(entry.average_latency_ms)}</strong><span className="cell-secondary">{t("Last")}: {formatDuration(entry.last_latency_ms)}</span></td>
            <td><strong>{formatPercent(entry.success_rate)}</strong><span className="cell-secondary">{t("Success / failed")}: {formatNumber(entry.successful_samples)} / {formatNumber(entry.failed_samples)}</span></td>
            <td><span className="cell-secondary">{t("Min")}: {formatDuration(entry.min_latency_ms)}</span><span className="cell-secondary">{t("Max")}: {formatDuration(entry.max_latency_ms)}</span></td>
            <td><strong>{formatNumber(entry.sample_count)}</strong></td>
            <td><span className="quality-last-sample"><StatusBadge status={qualityStatus(entry)} /><span className="cell-secondary">{formatDate(entry.last_sampled_at)}</span></span></td>
          </tr>)}</tbody>
        </table>
      </div>
    </Panel>
  );
}

function SortableHeader({
  label,
  sortKey,
  activeKey,
  direction,
  onSort,
  t,
}: {
  label: string;
  sortKey: SortKey;
  activeKey: SortKey;
  direction: "asc" | "desc";
  onSort: (key: SortKey) => void;
  t: (key: string) => string;
}) {
  const active = sortKey === activeKey;
  return <th aria-sort={active ? direction === "asc" ? "ascending" : "descending" : "none"}><button type="button" className="table-sort-button" onClick={() => onSort(sortKey)}>{t(label)}<ArrowDownUp size={13} aria-hidden="true" /><span className="sr-only">{active ? (direction === "asc" ? t("Ascending") : t("Descending")) : t("Sort")}</span></button></th>;
}
