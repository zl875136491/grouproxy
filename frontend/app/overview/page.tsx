"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, FileClock, Network } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import {
  getNodes,
  getOverview,
  getReleases,
  getSites,
  type Node,
  type Site,
} from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { ControlPlaneTopology } from "../../components/control-plane-topology";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Panel, StatusBadge } from "../../components/ui";
import { notifyToast } from "../../components/toast";

const activeReleaseStates = new Set(["queued", "applying", "health_check", "rolling_back"]);

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
  const { t, formatDate, formatNumber } = usePreferences();
  const session = useManagementSession();
  const overview = useQuery({ queryKey: ["overview"], queryFn: getOverview, enabled: session === true, refetchInterval: 10_000 });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, refetchInterval: 10_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, refetchInterval: 10_000 });
  const releases = useQuery({ queryKey: ["releases"], queryFn: () => getReleases(), enabled: session === true, refetchInterval: 5_000 });
  const lastReportedDrift = useRef<number | null>(null);
  const [selectedSiteId, setSelectedSiteId] = useState("");

  useEffect(() => {
    const availableSites = sites.data || [];
    if (!selectedSiteId && availableSites[0]) setSelectedSiteId(availableSites[0].id);
    if (selectedSiteId && !availableSites.some((site) => site.id === selectedSiteId)) setSelectedSiteId(availableSites[0]?.id || "");
  }, [selectedSiteId, sites.data]);

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
  const releaseItems = releases.data || [];
  const overviewData = overview.data;
  const activeReleases = releaseItems.filter((item) => activeReleaseStates.has(item.status));
  const nodesBySite = new Map(siteItems.map((site) => [site.id, nodeItems.filter((node) => node.site_id === site.id)]));
  const siteNames = new Map(siteItems.map((site) => [site.id, site.name]));
  const topologySites = siteItems.map((site) => {
    const siteNodes = nodesBySite.get(site.id) || [];
    return { site, state: siteState(site, siteNodes), nodeCount: siteNodes.length };
  });
  const selectedSite = siteItems.find((site) => site.id === selectedSiteId) || null;
  const selectedSiteNodes = selectedSite ? (nodesBySite.get(selectedSite.id) || []) : [];

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="OPERATIONS"
        title="Overview"
        description="Regional proxy state and deployment activity."
        actions={<Link className="button button-primary button-md" href="/sites"><Network size={16} /> {t("Manage policy")}</Link>}
      />

      <section className="metric-grid" aria-label={t("Control-plane summary")}>
        <Panel className="metric-panel"><span>{t("Nodes online")}</span><strong>{formatNumber(overviewData.online_nodes)}<small> / {formatNumber(overviewData.nodes)}</small></strong><em>{t("Heartbeat state")}</em></Panel>
        <Panel className="metric-panel"><span>{t("Configuration in sync")}</span><strong>{formatNumber(overviewData.in_sync_nodes)}<small> / {formatNumber(overviewData.nodes)}</small></strong><em>{t("Applied bundle matches")}</em></Panel>
        <Panel className="metric-panel"><span>{t("Drift or failure")}</span><strong>{formatNumber(overviewData.drifted_nodes)}</strong><em>{t("Requires operator review")}</em></Panel>
        <Panel className="metric-panel"><span>{t("Active connections")}</span><strong>{formatNumber(overviewData.connections)}</strong><em>{t("Control plane telemetry")}</em></Panel>
      </section>

      <section className="metric-grid metric-grid-trio" aria-label={t("Deployment and alert summary")}>
        <Panel className="metric-panel"><span>{t("Active deployments")}</span><strong>{formatNumber(activeReleases.length)}</strong><em>{t("Queued and running releases")}</em></Panel>
        <Link className="panel metric-panel metric-panel-link" href="/alerts"><span>{t("Open alerts")}</span><strong>{formatNumber(overviewData.open_alerts)}</strong><em>{t("Review active conditions")}</em></Link>
        <Link className="panel metric-panel metric-panel-link" href="/probes"><span>{t("Open circuits")}</span><strong>{formatNumber(overviewData.open_circuits)}</strong><em>{t("Review outbound health")}</em></Link>
      </section>

      <section className="dashboard-grid dashboard-grid-primary">
        <Panel className="topology-panel">
          <div className="panel-heading"><div><span className="panel-kicker">{t("REGIONAL TOPOLOGY")}</span><h2>{t("Control plane to edge sites")}</h2></div><Link href="/nodes">{t("Node inventory")} <ArrowRight size={15} /></Link></div>
          <ControlPlaneTopology sites={topologySites} onlineNodes={overviewData.online_nodes} totalNodes={overviewData.nodes} formatNumber={formatNumber} t={t} selectedSiteId={selectedSiteId} onSiteSelect={setSelectedSiteId} />
          {selectedSite ? <TopologySiteDetail site={selectedSite} nodes={selectedSiteNodes} state={topologySites.find((item) => item.site.id === selectedSite.id)?.state || "unknown"} formatNumber={formatNumber} t={t} /> : <div className="topology-site-empty">{t("Select a site in the topology to inspect its nodes.")}</div>}
        </Panel>

        <Panel className="activity-panel">
          <div className="panel-heading"><div><span className="panel-kicker">{t("DEPLOYMENT")}</span><h2>{t("Recent releases")}</h2></div><Link href="/releases">{t("View all")} <ArrowRight size={15} /></Link></div>
          {releases.isLoading ? <LoadingState rows={3} /> : releases.isError ? <ErrorState error="Release history is unavailable." onRetry={() => void releases.refetch()} /> : releaseItems.length ? (
            <div className="activity-list">
              {releaseItems.slice(0, 4).map((release) => <Link className="activity-row" href={`/releases?release=${release.release_id}`} key={release.release_id}><span className="activity-icon"><FileClock size={16} /></span><div><strong>{t(siteNames.get(release.site_id) || "Unknown site")}</strong><span>{t(release.stage.replaceAll("_", " "))} · {formatDate(release.created_at)}</span></div><StatusBadge status={release.status} /></Link>)}
            </div>
          ) : <EmptyState title="No releases yet" detail="Create a draft from a site policy workspace." />}
        </Panel>
      </section>

    </div>
  );
}

function TopologySiteDetail({
  site,
  nodes,
  state,
  formatNumber,
  t,
}: {
  site: Site;
  nodes: Node[];
  state: string;
  formatNumber: (value: number) => string;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  return <article className="topology-site-detail"><header><div><span className="panel-kicker">{t("SELECTED SITE")}</span><h3>{t(site.name)}</h3><span className="mono">{site.slug}</span></div><div className="topology-site-detail-actions"><StatusBadge status={state} /><Link href={`/sites/${site.slug}/cidrs`}>{t("Open policy")} <ArrowRight size={14} /></Link></div></header><div className="topology-site-facts"><div><span>{t("Nodes")}</span><strong>{formatNumber(nodes.length)}</strong></div><div><span>{t("Online")}</span><strong>{formatNumber(nodes.filter((node) => node.liveness_status === "online").length)}</strong></div><div><span>{t("In sync")}</span><strong>{formatNumber(nodes.filter((node) => node.config_status === "in_sync").length)}</strong></div></div>{nodes.length ? <div className="topology-node-list">{nodes.map((node) => <div className="topology-node-row" key={node.id}><div><strong>{node.name}</strong><small className="mono">{node.agent_id}</small></div><div><StatusBadge status={node.liveness_status} /><StatusBadge status={node.config_status} /></div></div>)}</div> : <p className="topology-site-empty">{t("No node enrolled")}</p>}</article>;
}
