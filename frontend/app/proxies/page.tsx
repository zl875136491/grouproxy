"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, CircleAlert, Filter, Gauge, RefreshCw, Search, Server, Waypoints, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  getNodes,
  getProxyConfigs,
  getSites,
  selectNodeProxy,
  type Node,
  type ProxyConfigSnapshot,
  type ProxyEndpoint,
  type ProxyGroup,
  type Release,
  type Site,
} from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { cn } from "../../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, IconButton, Panel, StatusBadge } from "../../components/ui";

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
  return labels[normalized] ? t(labels[normalized]) : value;
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

type EndpointFilter = "" | "online" | "degraded" | "offline" | "pending";

function ProxyEndpointFilters({
  search,
  onSearch,
  status,
  onStatus,
  shown,
  total,
  t,
}: {
  search: string;
  onSearch: (value: string) => void;
  status: EndpointFilter;
  onStatus: (value: EndpointFilter) => void;
  shown: number;
  total: number;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  return (
    <div className="proxy-endpoint-filters" role="search">
      <label className="filter-search">
        <Search size={15} aria-hidden="true" />
        <span className="sr-only">{t("Search proxy services")}</span>
        <input
          value={search}
          onChange={(event) => onSearch(event.target.value)}
          placeholder={t("Search proxy services")}
        />
        {search ? (
          <button
            type="button"
            className="icon-button"
            aria-label={t("Clear search")}
            title={t("Clear search")}
            onClick={() => onSearch("")}
          >
            <X size={14} />
          </button>
        ) : null}
      </label>
      <label className="select-control list-filter-select">
        <Filter size={15} aria-hidden="true" />
        <span>{t("State")}</span>
        <span className="select-trigger">
          <select
            value={status}
            aria-label={t("Filter proxy state")}
            onChange={(event) => onStatus(event.target.value as EndpointFilter)}
          >
            <option value="">{t("All endpoint states")}</option>
            <option value="online">{t("online")}</option>
            <option value="degraded">{t("degraded")}</option>
            <option value="offline">{t("offline")}</option>
            <option value="pending">{t("pending")}</option>
          </select>
          <ChevronDown size={14} aria-hidden="true" />
        </span>
      </label>
      <span className="toolbar-note">{t("{count} of {total} shown", { count: shown, total })}</span>
    </div>
  );
}

function LatencyHistory({
  history,
  formatDuration,
  t,
}: {
  history: ProxyEndpoint["history"];
  formatDuration: (value: number | null | undefined) => string;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  const points = history.slice(-10);
  return (
    <div className="proxy-history" aria-label={t("Recent latency history")}>
      {points.map((point, index) => {
        const label = point.delay_ms === null || point.delay_ms === undefined
          ? t("No latency")
          : formatDuration(point.delay_ms);
        return (
          <span
            className={cn("proxy-history-dot", `proxy-history-${latencyHistoryTone(point.delay_ms)}`)}
            key={`${point.at || "point"}-${index}`}
            title={label}
            aria-label={label}
          />
        );
      })}
    </div>
  );
}

function SnapshotState({
  snapshot,
  formatDate,
  t,
}: {
  snapshot: ProxyConfigSnapshot | undefined;
  formatDate: (value: string | null | undefined, withTime?: boolean) => string;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  if (!snapshot) return <StatusBadge status="pending" />;
  if (!snapshot.api_available) return <StatusBadge status="offline" />;
  const sampled = new Date(snapshot.sampled_at).getTime();
  const stale = Number.isFinite(sampled) && Date.now() - sampled > 90_000;
  return (
    <span className="proxy-snapshot-state">
      <StatusBadge status={stale ? "degraded" : "online"} />
      <small>{t("Updated {date}", { date: formatDate(snapshot.sampled_at) })}</small>
    </span>
  );
}

function EndpointCard({
  endpoint,
  selected,
  formatDuration,
  onSelect,
  t,
}: {
  endpoint: ProxyEndpoint;
  selected: boolean;
  formatDuration: (value: number | null | undefined) => string;
  onSelect: () => void;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  const status = endpointStatus(endpoint);
  const delay = endpoint.delay_ms === null || endpoint.delay_ms === undefined
    ? t("No latency")
    : formatDuration(endpoint.delay_ms);
  return (
    <button
      type="button"
      className={cn("proxy-endpoint-card", selected && "proxy-endpoint-selected", `proxy-endpoint-${status}`)}
      onClick={onSelect}
      aria-pressed={selected}
      title={t("Select {name}", { name: endpoint.name })}
    >
      <span className="proxy-endpoint-heading">
        <span className="proxy-endpoint-dot" aria-hidden="true" />
        <strong title={endpoint.name}>{endpoint.name}</strong>
        {selected ? <span className="proxy-current-mark">{t("Current")}</span> : null}
      </span>
      <span className="proxy-endpoint-meta">
        <span>{proxyTypeLabel(endpoint.type, t)}</span>
        <span>{t(endpoint.udp ? "UDP" : "TCP")}</span>
        <StatusBadge status={status} />
      </span>
      <span className="proxy-endpoint-delay">{delay}</span>
      <LatencyHistory history={endpoint.history} formatDuration={formatDuration} t={t} />
    </button>
  );
}

function OutboundServiceCard({
  group,
  formatDuration,
  onSelect,
  endpointSearch,
  endpointStatusFilter,
  onEndpointSearch,
  onEndpointStatus,
  t,
}: {
  group: ProxyGroup;
  formatDuration: (value: number | null | undefined) => string;
  onSelect: (endpoint: ProxyEndpoint) => void;
  endpointSearch: string;
  endpointStatusFilter: EndpointFilter;
  onEndpointSearch: (value: string) => void;
  onEndpointStatus: (value: EndpointFilter) => void;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  const endpointByName = new Map(group.nodes.map((endpoint) => [endpoint.name, endpoint]));
  const endpoints = group.all.map(
    (name) => endpointByName.get(name) || {
      name,
      type: "unknown",
      udp: false,
      alive: null,
      delay_ms: null,
      history: [],
    },
  );
  const normalizedSearch = endpointSearch.trim().toLocaleLowerCase();
  const filteredEndpoints = endpoints.filter((endpoint) => {
    const matchesSearch = !normalizedSearch || endpoint.name.toLocaleLowerCase().includes(normalizedSearch);
    const matchesStatus = !endpointStatusFilter || endpointStatus(endpoint) === endpointStatusFilter;
    return matchesSearch && matchesStatus;
  });
  return (
    <article className="proxy-group-card">
      <header className="proxy-group-heading">
        <div className="proxy-group-title">
          <span className="proxy-group-icon" aria-hidden="true"><Waypoints size={16} /></span>
          <div>
            <strong>{t("Outbound services")}</strong>
            <span>{t("{count} services", { count: endpoints.length })}</span>
          </div>
        </div>
        <StatusBadge status={groupStatus(group)} />
      </header>
      <div className="proxy-group-current">
        <span className="proxy-current-label">{t("Active service")}</span>
        <strong>{group.now || t("No selection")}</strong>
        {group.delay_ms !== null && group.delay_ms !== undefined
          ? <span className="proxy-group-delay">{formatDuration(group.delay_ms)}</span>
          : null}
      </div>
      <ProxyEndpointFilters
        search={endpointSearch}
        onSearch={onEndpointSearch}
        status={endpointStatusFilter}
        onStatus={onEndpointStatus}
        shown={filteredEndpoints.length}
        total={endpoints.length}
        t={t}
      />
      {filteredEndpoints.length ? <div className="proxy-endpoint-grid">
        {filteredEndpoints.map((endpoint) => (
          <EndpointCard
            key={endpoint.name}
            endpoint={endpoint}
            selected={Boolean(group.now && endpoint.name === group.now)}
            formatDuration={formatDuration}
            onSelect={() => onSelect(endpoint)}
            t={t}
          />
        ))}
      </div> : <div className="proxy-filter-empty"><Search size={17} /><strong>{t("No matching proxy services")}</strong><span>{t("Try another name or state filter.")}</span></div>}
    </article>
  );
}

function NodeProxyPanel({
  node,
  snapshot,
  formatDate,
  formatDuration,
  formatNumber,
  onSelect,
  endpointSearch,
  endpointStatusFilter,
  onEndpointSearch,
  onEndpointStatus,
  t,
}: {
  node: Node;
  snapshot: ProxyConfigSnapshot | undefined;
  formatDate: (value: string | null | undefined, withTime?: boolean) => string;
  formatDuration: (value: number | null | undefined) => string;
  formatNumber: (value: number | null | undefined) => string;
  onSelect: (endpoint: ProxyEndpoint) => void;
  endpointSearch: string;
  endpointStatusFilter: EndpointFilter;
  onEndpointSearch: (value: string) => void;
  onEndpointStatus: (value: EndpointFilter) => void;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  const selectionGroup = snapshot?.groups.find((group) => group.name.trim().toLocaleLowerCase() === "subscription");
  const apiAvailable = snapshot?.api_available ?? false;
  return (
    <Panel className="proxy-node-panel">
      <header className="proxy-node-heading">
        <div className="proxy-node-title">
          <span className="proxy-node-icon" aria-hidden="true"><Server size={17} /></span>
          <div><h2>{node.name}</h2><span className="mono">{node.agent_id}</span></div>
        </div>
        <SnapshotState snapshot={snapshot} formatDate={formatDate} t={t} />
      </header>
      {snapshot?.error
        ? <div className="proxy-inline-warning"><CircleAlert size={15} /><span>{t(snapshot.error)}</span></div>
        : null}
      {selectionGroup?.all.length
        ? <div className={cn("proxy-groups", !apiAvailable && "proxy-groups-stale")}>
            {!apiAvailable ? <div className="proxy-stale-note">{t("Showing last successful snapshot.")}</div> : null}
            <div className="proxy-selection-note">{t("Choose an outbound below. The selection creates a node release and is applied by its monitor.")}</div>
            <OutboundServiceCard group={selectionGroup} formatDuration={formatDuration} onSelect={onSelect} endpointSearch={endpointSearch} endpointStatusFilter={endpointStatusFilter} onEndpointSearch={onEndpointSearch} onEndpointStatus={onEndpointStatus} t={t} />
          </div>
        : <EmptyState
            title={apiAvailable ? "No outbound services reported." : "Proxy API is unavailable."}
            detail={apiAvailable ? "The monitor has not reported its subscription selector yet." : "The control plane will show the last successful snapshot when the monitor reconnects."}
          />}
      <footer className="proxy-node-footer">
        <span>{t("{count} services", { count: formatNumber(selectionGroup?.all.length || 0) })}</span>
        <span>{snapshot ? t("Received {date}", { date: formatDate(snapshot.received_at) }) : t("Awaiting monitor data")}</span>
      </footer>
    </Panel>
  );
}

type PendingSelection = {
  node: Node;
  endpoint: ProxyEndpoint;
};

export default function ProxiesPage() {
  const { t, formatDate, formatDuration, formatNumber } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [selectedSiteId, setSelectedSiteId] = useState("");
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const [endpointSearch, setEndpointSearch] = useState("");
  const [endpointStatusFilter, setEndpointStatusFilter] = useState<EndpointFilter>("");
  const [pendingSelection, setPendingSelection] = useState<PendingSelection | null>(null);
  const [selectionNotice, setSelectionNotice] = useState<Release | null>(null);
  const configs = useQuery({ queryKey: ["proxy-configs"], queryFn: () => getProxyConfigs(), enabled: session === true, refetchInterval: 5_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, staleTime: 10_000 });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  const snapshots = useMemo(() => new Map((configs.data || []).map((snapshot) => [snapshot.node_id, snapshot])), [configs.data]);
  const siteItems = sites.data || [];
  const inventoryReady = sites.isSuccess && nodes.isSuccess;
  const initialSiteId = useMemo(() => {
    const enrolledSite = siteItems.find((site) => (nodes.data || []).some((node) => node.site_id === site.id));
    return enrolledSite?.id || siteItems[0]?.id || "";
  }, [nodes.data, siteItems]);
  const siteNodes = useMemo(() => (nodes.data || []).filter((node) => node.site_id === selectedSiteId), [nodes.data, selectedSiteId]);
  const selectedSite = siteItems.find((site) => site.id === selectedSiteId) || null;
  const selectedNode = siteNodes.find((node) => node.id === selectedNodeId) || siteNodes[0] || null;
  const selectedSnapshot = selectedNode ? snapshots.get(selectedNode.agent_id) : undefined;
  const selectMutation = useMutation({
    mutationFn: (selection: PendingSelection) => selectNodeProxy(selection.node.id, {
      group: "subscription",
      outbound: selection.endpoint.name,
      expected_current_version: selection.node.desired_version || null,
      note: "Selected from proxy operations view",
    }),
    onSuccess: async (release) => {
      setPendingSelection(null);
      setSelectionNotice(release);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["proxy-configs"] }),
        queryClient.invalidateQueries({ queryKey: ["nodes"] }),
        queryClient.invalidateQueries({ queryKey: ["releases"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      ]);
    },
  });

  useEffect(() => {
    if (!inventoryReady) return;
    if (!selectedSiteId && initialSiteId) setSelectedSiteId(initialSiteId);
    if (selectedSiteId && !siteItems.some((site) => site.id === selectedSiteId)) setSelectedSiteId(initialSiteId);
  }, [initialSiteId, inventoryReady, selectedSiteId, siteItems]);
  useEffect(() => {
    if (!selectedNodeId || !siteNodes.some((node) => node.id === selectedNodeId)) {
      setSelectedNodeId(siteNodes[0]?.id || "");
      setEndpointSearch("");
      setEndpointStatusFilter("");
    }
  }, [selectedNodeId, siteNodes]);

  if (session === null) return <LoadingState rows={8} />;
  if (!session) return <SessionGate />;
  if (configs.isLoading || nodes.isLoading || sites.isLoading) return <LoadingState rows={8} />;
  if (configs.isError || nodes.isError || sites.isError) {
    const issue = configs.error || nodes.error || sites.error;
    return <ErrorState error={issue instanceof Error ? issue.message : "Unable to load proxy configuration."} onRetry={() => void Promise.all([configs.refetch(), nodes.refetch(), sites.refetch()])} />;
  }

  const snapshot = selectedSnapshot;
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="OPERATE"
        title="Outbound services"
        description="Inspect a site and node, then choose the outbound service that its monitor should apply."
        actions={<IconButton label={t("Refresh")} onClick={() => void Promise.all([configs.refetch(), nodes.refetch(), sites.refetch()])}><RefreshCw size={16} /></IconButton>}
      />
      <Panel className="proxy-toolbar-panel">
        <div className="proxy-switcher-block">
          <div className="proxy-switcher-heading"><div><span className="panel-kicker">{t("SITE")}</span><strong>{t("Choose a site")}</strong></div><span className="toolbar-note">{t("{count} sites", { count: formatNumber(siteItems.length) })}</span></div>
          <div className="segmented-control proxy-site-tabs" role="tablist" aria-label={t("Sites")}>
            {siteItems.map((site) => {
              const active = site.id === selectedSiteId;
              const count = (nodes.data || []).filter((node) => node.site_id === site.id).length;
              return <button key={site.id} type="button" role="tab" aria-selected={active} className={active ? "segmented-active" : ""} onClick={() => { setSelectedSiteId(site.id); setSelectedNodeId(""); setEndpointSearch(""); setEndpointStatusFilter(""); setSelectionNotice(null); }}>{t(site.name)}<span className="tab-count">{formatNumber(count)}</span></button>;
            })}
          </div>
          {selectedSite && siteNodes.length > 1 ? <>
            <div className="proxy-switcher-heading proxy-node-switcher-heading"><div><span className="panel-kicker">{t("NODE")}</span><strong>{t("Choose a node")}</strong></div><span className="toolbar-note">{t("{count} nodes", { count: formatNumber(siteNodes.length) })}</span></div>
            <div className="segmented-control proxy-node-tabs" role="tablist" aria-label={t("Nodes")}>
              {siteNodes.map((node) => <button key={node.id} type="button" role="tab" aria-selected={node.id === selectedNode?.id} className={node.id === selectedNode?.id ? "segmented-active" : ""} onClick={() => { setSelectedNodeId(node.id); setEndpointSearch(""); setEndpointStatusFilter(""); setSelectionNotice(null); }}>{node.name}<StatusBadge status={node.liveness_status} /></button>)}
            </div>
          </> : null}
        </div>
        <div className="proxy-toolbar-foot"><span className="toolbar-title"><Gauge size={18} />{selectedSite ? t(selectedSite.name) : t("No site selected")}</span><span className="toolbar-note">{t("A selection is released through the monitor ACK workflow.")}</span></div>
      </Panel>
      {selectionNotice ? <div className="change-summary"><span>{t("Release queued for {node}.", { node: selectionNotice.node_ids[0] || t("the node") })}</span><StatusBadge status={selectionNotice.status} /></div> : null}
      {selectedNode ? <NodeProxyPanel node={selectedNode} snapshot={snapshot} formatDate={formatDate} formatDuration={formatDuration} formatNumber={formatNumber} endpointSearch={endpointSearch} endpointStatusFilter={endpointStatusFilter} onEndpointSearch={setEndpointSearch} onEndpointStatus={setEndpointStatusFilter} onSelect={(endpoint) => setPendingSelection({ node: selectedNode, endpoint })} t={t} /> : <Panel><EmptyState title="No nodes enrolled." detail="Enroll a regional monitor before choosing an outbound service." /></Panel>}
      <ConfirmDialog
        open={Boolean(pendingSelection)}
        onOpenChange={(open) => { if (!open && !selectMutation.isPending) setPendingSelection(null); }}
        title="Switch proxy service"
        description={pendingSelection ? t("Set {node} to use {outbound}. This creates a node release; the monitor applies it after validation and health checks.", { node: pendingSelection.node.name, outbound: pendingSelection.endpoint.name }) : ""}
        confirmLabel="Create release"
        busy={selectMutation.isPending}
        onConfirm={() => pendingSelection && selectMutation.mutate(pendingSelection)}
      />
      {selectMutation.error ? <div className="inline-error" role="alert">{selectMutation.error instanceof Error ? t(selectMutation.error.message) : t("The proxy selection could not be released.")}</div> : null}
    </div>
  );
}
