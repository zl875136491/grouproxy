"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, ArrowRight, FileClock, Network, ServerCog, ShieldCheck } from "lucide-react";
import Link from "next/link";
import {
  getNodes,
  getOverview,
  getReleases,
  getSites,
  getTasks,
  type Node,
  type Site,
} from "../lib/api";
import { usePreferences } from "../lib/preferences";
import { label } from "../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../components/data-state";
import { PageHeader } from "../components/page-header";
import { SessionGate, useManagementSession } from "../components/session-gate";
import { Panel, StatusBadge } from "../components/ui";

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
  const { t, formatDate, formatNumber, formatPercent } = usePreferences();
  const session = useManagementSession();
  const overview = useQuery({ queryKey: ["overview"], queryFn: getOverview, enabled: session === true, refetchInterval: 10_000 });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, refetchInterval: 10_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, refetchInterval: 10_000 });
  const releases = useQuery({ queryKey: ["releases"], queryFn: () => getReleases(), enabled: session === true, refetchInterval: 5_000 });
  const tasks = useQuery({ queryKey: ["tasks"], queryFn: () => getTasks(), enabled: session === true, refetchInterval: 5_000 });

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
  const taskItems = tasks.data || [];
  const overviewData = overview.data;
  const activeReleases = releaseItems.filter((item) => activeReleaseStates.has(item.status));
  const activeTasks = taskItems.filter((item) => ["queued", "running", "cancel_requested"].includes(item.status));
  const nodesBySite = new Map(siteItems.map((site) => [site.id, nodeItems.filter((node) => node.site_id === site.id)]));
  const siteNames = new Map(siteItems.map((site) => [site.id, site.name]));

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="OPERATIONS"
        title="Overview"
        description="Regional proxy state and deployment activity."
        actions={<Link className="button button-primary button-md" href="/sites"><Network size={16} /> {t("Manage policy")}</Link>}
      />

      {overviewData.drifted_nodes > 0 ? (
        <div className="alert-strip alert-danger" role="alert">
          <ShieldCheck size={18} />
          <div><strong>{t("{count} nodes need attention", { count: formatNumber(overviewData.drifted_nodes) })}</strong><span>{t("Configuration or service state differs from the desired release.")}</span></div>
          <Link href="/nodes">{t("Review nodes")} <ArrowRight size={15} /></Link>
        </div>
      ) : null}

      <section className="metric-grid" aria-label={t("Control-plane summary")}>
        <Panel className="metric-panel"><span>{t("Nodes online")}</span><strong>{formatNumber(overviewData.online_nodes)}<small> / {formatNumber(overviewData.nodes)}</small></strong><em>{t("Heartbeat state")}</em></Panel>
        <Panel className="metric-panel"><span>{t("Configuration in sync")}</span><strong>{formatNumber(overviewData.in_sync_nodes)}<small> / {formatNumber(overviewData.nodes)}</small></strong><em>{t("Applied bundle matches")}</em></Panel>
        <Panel className="metric-panel"><span>{t("Drift or failure")}</span><strong>{formatNumber(overviewData.drifted_nodes)}</strong><em>{t("Requires operator review")}</em></Panel>
        <Panel className="metric-panel"><span>{t("Active connections")}</span><strong>{formatNumber(overviewData.connections)}</strong><em>{t("Control plane telemetry")}</em></Panel>
      </section>

      <section className="metric-grid metric-grid-trio" aria-label={t("Deployment and alert summary")}>
        <Panel className="metric-panel"><span>{t("Active deployments")}</span><strong>{formatNumber(activeReleases.length)}</strong><em>{t("{count} queued or running tasks", { count: formatNumber(activeTasks.length) })}</em></Panel>
        <Link className="panel metric-panel metric-panel-link" href="/alerts"><span>{t("Open alerts")}</span><strong>{formatNumber(overviewData.open_alerts)}</strong><em>{t("Review active conditions")}</em></Link>
        <Link className="panel metric-panel metric-panel-link" href="/probes"><span>{t("Open circuits")}</span><strong>{formatNumber(overviewData.open_circuits)}</strong><em>{t("Review outbound health")}</em></Link>
      </section>

      <section className="dashboard-grid dashboard-grid-primary">
        <Panel className="topology-panel">
          <div className="panel-heading"><div><span className="panel-kicker">{t("REGIONAL TOPOLOGY")}</span><h2>{t("Control plane to edge sites")}</h2></div><Link href="/nodes">{t("Node inventory")} <ArrowRight size={15} /></Link></div>
          <div className="topology-canvas">
            <div className="topology-control"><span className="topology-control-icon"><ServerCog size={19} /></span><div><strong>codedev</strong><span>{t("Control plane")}</span></div></div>
            <div className="topology-branches" aria-label={t("Site topology")}>
              {siteItems.map((site) => {
                const siteNodes = nodesBySite.get(site.id) || [];
                const state = siteState(site, siteNodes);
                return (
                  <Link className="topology-site" href={`/sites/${site.slug}/cidrs`} key={site.id}>
                    <span className={`topology-dot topology-${state.replaceAll(" ", "-")}`} />
                    <div><strong>{t(site.name)}</strong><span>{siteNodes.length ? `${formatNumber(siteNodes.length)} ${t(siteNodes.length === 1 ? "node" : "nodes")}` : t("No node enrolled")}</span></div>
                    <StatusBadge status={state} />
                  </Link>
                );
              })}
            </div>
          </div>
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

      <section className="dashboard-grid dashboard-grid-secondary">
        <Panel>
          <div className="panel-heading"><div><span className="panel-kicker">{t("EDGE INVENTORY")}</span><h2>{t("Node state")}</h2></div><Link href="/nodes">{t("Open nodes")} <ArrowRight size={15} /></Link></div>
          <div className="table-wrap"><table><thead><tr><th>{t("Node")}</th><th>{t("Liveness")}</th><th>{t("Config")}</th><th>{t("Service")}</th><th>{t("Applied / desired")}</th></tr></thead><tbody>{nodeItems.map((node) => <tr key={node.id}><td><strong>{node.name}</strong><span className="cell-secondary">{t(siteNames.get(node.site_id) || node.site_id)}</span></td><td><StatusBadge status={node.liveness_status} /></td><td><StatusBadge status={node.config_status} /></td><td><StatusBadge status={node.service_status} /></td><td>{formatNumber(node.applied_version)} / {formatNumber(node.desired_version)}</td></tr>)}</tbody></table></div>
        </Panel>
        <Panel>
          <div className="panel-heading"><div><span className="panel-kicker">{t("TASK QUEUE")}</span><h2>{t("Active work")}</h2></div><Link href="/tasks">{t("Open queue")} <ArrowRight size={15} /></Link></div>
          {tasks.isLoading ? <LoadingState rows={3} /> : tasks.isError ? <ErrorState error="Task queue is unavailable." onRetry={() => void tasks.refetch()} /> : activeTasks.length ? <div className="activity-list">{activeTasks.slice(0, 5).map((task) => <Link className="activity-row" href="/tasks" key={task.task_id}><span className="activity-icon"><Activity size={16} /></span><div><strong>{t(label(task.task_type))}</strong><span>{t(task.stage.replaceAll("_", " "))} · {formatPercent(task.progress)}</span></div><StatusBadge status={task.status} /></Link>)}</div> : <EmptyState title="No active tasks" detail="Queued and running jobs appear here." />}
        </Panel>
      </section>
    </div>
  );
}
