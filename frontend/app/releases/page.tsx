"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarRange, Check, Clipboard, FileCheck2, FileDiff, Filter, MapPinned, Play, RotateCcw, Server } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import {
  getDrafts,
  getRelease,
  getReleaseDetail,
  getReleases,
  getNodes,
  getSites,
  publishRelease,
  type Draft,
  type Release,
  type ReleaseDetail as ReleaseDetailData,
} from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { shortHash } from "../../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { FilterSelect } from "../../components/list-filters";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { useToast } from "../../components/toast";
import { Button, ConfirmDialog, Panel, StatusBadge } from "../../components/ui";

const releaseStages = ["draft", "validated", "queued", "applying", "health_check", "succeeded"];
type ReleaseFilter = "" | "applying" | "succeeded" | "failed";
type RangeFilter = "24h" | "7d" | "30d" | "all";
type ReleaseListView = "drafts" | "history";

function stagePosition(stage: string) {
  const index = releaseStages.indexOf(stage);
  return index < 0 ? 0 : index;
}

function rangeStart(value: RangeFilter): string | undefined {
  if (value === "all") return undefined;
  const hours = value === "24h" ? 24 : value === "7d" ? 24 * 7 : 24 * 30;
  return new Date(Date.now() - hours * 60 * 60 * 1000).toISOString();
}

export default function ReleasesPage() {
  return <Suspense fallback={<LoadingState rows={9} />}><ReleasesWorkspace /></Suspense>;
}

function ReleasesWorkspace() {
  const { t, formatDate, formatNumber } = usePreferences();
  const session = useManagementSession();
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const draftId = searchParams.get("draft");
  const releaseId = searchParams.get("release");
  const [publishTarget, setPublishTarget] = useState<Draft | null>(null);
  const [listView, setListView] = useState<ReleaseListView>(() => draftId ? "drafts" : "history");
  const [siteFilter, setSiteFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<ReleaseFilter>("");
  const [rangeFilter, setRangeFilter] = useState<RangeFilter>("30d");
  // Keep the absolute boundary stable until the operator changes the range.
  // Otherwise each render produces a new React Query key and continuously
  // restarts the release request.
  const since = useMemo(() => rangeStart(rangeFilter), [rangeFilter]);
  const drafts = useQuery({ queryKey: ["drafts"], queryFn: getDrafts, enabled: session === true, refetchInterval: 5_000 });
  const releases = useQuery({ queryKey: ["releases", siteFilter, statusFilter, since], queryFn: () => getReleases({ siteId: siteFilter || undefined, status: statusFilter || undefined, since }), enabled: session === true, refetchInterval: 3_000 });
  // A deep link must be authoritative even when the release is outside the
  // list's current time/status filters. The list remains useful for browsing,
  // but it is not a reliable source for a specifically requested release.
  const requestedRelease = useQuery({
    queryKey: ["release", releaseId],
    queryFn: () => getRelease(releaseId!),
    enabled: session === true && Boolean(releaseId),
    placeholderData: undefined,
    refetchInterval: 3_000,
  });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, staleTime: 30_000 });
  const siteNames = useMemo(() => new Map((sites.data || []).map((site) => [site.id, site.name])), [sites.data]);
  const nodeLabels = useMemo(
    () => {
      const labels = new Map<string, string>();
      for (const node of nodes.data || []) {
        labels.set(node.id, node.name);
        labels.set(node.agent_id, node.name);
      }
      return labels;
    },
    [nodes.data],
  );
  const draftItems = drafts.data || [];
  const releaseItems = releases.data || [];
  const selectedDraft = useMemo(() => draftItems.find((item) => item.id === draftId) || null, [draftId, draftItems]);
  const selectedRelease = useMemo(
    () => {
      // A release deep link is an explicit selection. Never fall back to the
      // first filtered history row while its authoritative request is still
      // loading (or after it fails), otherwise an operator can see a stale
      // failure for a different release.
      if (releaseId) return requestedRelease.data || null;
      return draftId ? null : releaseItems[0] || null;
    },
    [draftId, releaseId, releaseItems, requestedRelease.data],
  );
  const releaseDetail = useQuery({ queryKey: ["release-detail", selectedRelease?.release_id], queryFn: () => getReleaseDetail(selectedRelease!.release_id), enabled: Boolean(selectedRelease), refetchInterval: selectedRelease?.status === "succeeded" || selectedRelease?.status === "failed" ? false : 2_000 });

  useEffect(() => {
    if (draftId) setListView("drafts");
    else if (releaseId) setListView("history");
  }, [draftId, releaseId]);
  const publish = useMutation({
    mutationFn: (draft: Draft) => publishRelease({ draft_id: draft.id, site_id: draft.site_id, node_ids: draft.node_ids, note: "Published from operations console" }),
    onSuccess: async (release) => {
      setPublishTarget(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["drafts"] }),
        queryClient.invalidateQueries({ queryKey: ["releases"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      ]);
      router.replace(`/releases?release=${release.release_id}`);
    },
  });

  if (session === null) return <LoadingState rows={9} />;
  if (!session) return <SessionGate />;
  if (drafts.isLoading || releases.isLoading || sites.isLoading) return <LoadingState rows={9} />;
  if (drafts.isError || releases.isError || sites.isError) {
    const issue = drafts.error || releases.error || sites.error;
    return <ErrorState error={issue instanceof Error ? issue.message : "Unable to load release state."} onRetry={() => void Promise.all([drafts.refetch(), releases.refetch(), sites.refetch()])} />;
  }
  if (releaseId && requestedRelease.isError) {
    return <ErrorState error={requestedRelease.error instanceof Error ? requestedRelease.error.message : "Unable to load the requested release."} onRetry={() => void requestedRelease.refetch()} />;
  }

  const openDrafts = draftItems.filter((draft) => draft.status === "draft" && (!siteFilter || draft.site_id === siteFilter));
  return (
    <div className="page-stack page-fill releases-page">
      <section className="release-hero">
        <div><span className="release-hero-eyebrow">{t("DEPLOY")}</span><h1>{t("Recent releases")}</h1><p>{t("Review desired state, node ACKs, and the path from draft to applied configuration.")}</p></div>
      </section>
      <section className="release-workspace">
        <div className="release-list-column">
          <Panel className="release-filter-panel">
            <div className="release-filter-heading"><div className="toolbar-title"><Filter size={16} /><span>{t("Filter releases")}</span></div><span className="toolbar-note">{t("{count} shown", { count: formatNumber(releaseItems.length) })}</span></div>
            <div className="release-filters">
              <FilterSelect label="Site" value={siteFilter} setValue={setSiteFilter} icon={<MapPinned size={15} aria-hidden="true" />} options={[{ value: "", label: "All sites" }, ...(sites.data || []).map((site) => ({ value: site.id, label: site.name }))]} />
              <FilterSelect label="State" value={statusFilter} setValue={(value) => setStatusFilter(value as ReleaseFilter)} options={[{ value: "", label: "All states" }, { value: "applying", label: "applying" }, { value: "succeeded", label: "succeeded" }, { value: "failed", label: "failed" }]} />
              <FilterSelect label="Time range" value={rangeFilter} setValue={(value) => setRangeFilter(value as RangeFilter)} icon={<CalendarRange size={15} aria-hidden="true" />} options={[{ value: "24h", label: "Last 24 hours" }, { value: "7d", label: "Last 7 days" }, { value: "30d", label: "Last 30 days" }, { value: "all", label: "All time" }]} />
            </div>
          </Panel>
          <Panel className="release-browser-panel">
            <div className="release-browser-heading">
              <div className="release-list-actions">
                <div className="segmented-control" role="tablist" aria-label={t("Releases")}>
                  <button type="button" role="tab" aria-selected={listView === "drafts"} className={listView === "drafts" ? "segmented-active" : ""} onClick={() => setListView("drafts")}>{t("Drafts")}</button>
                  <button type="button" role="tab" aria-selected={listView === "history"} className={listView === "history" ? "segmented-active" : ""} onClick={() => setListView("history")}>{t("Release history")}</button>
                </div>
              </div>
            </div>
            {listView === "drafts" ? (
              openDrafts.length ? <div className="release-list release-scroll-list">{openDrafts.map((draft) => <div className={cnReleaseRow(selectedDraft?.id === draft.id)} key={draft.id}><button type="button" onClick={() => router.replace(`/releases?draft=${draft.id}`)}><span><strong>{t(siteNames.get(draft.site_id) || draft.site_id)}</strong><small>{t("Revision v{revision} · {date}", { revision: formatNumber(draft.source_revision), date: formatDate(draft.created_at) })}</small></span><StatusBadge status={draft.risk_level} /></button><Button size="sm" variant="primary" onClick={() => setPublishTarget(draft)}><Play size={14} />{t("Publish")}</Button></div>)}</div> : <EmptyState title="No publishable drafts" detail="Create a draft from a site policy workspace." />
            ) : (
              releaseItems.length ? <div className="release-list release-scroll-list">{releaseItems.map((release) => <button type="button" className={`release-history-row release-history-card ${selectedRelease?.release_id === release.release_id ? "release-row-selected" : ""}`} onClick={() => router.replace(`/releases?release=${release.release_id}`)} key={release.release_id}><span className="release-history-icon"><FileCheck2 size={17} /></span><span><strong>{t(siteNames.get(release.site_id) || release.site_id)}</strong><small>{formatDate(release.created_at)} · {t("{count} nodes", { count: formatNumber(release.node_ids.length) })}</small></span><StatusBadge status={release.status} /></button>)}</div> : <EmptyState title="No releases recorded" />
            )}
          </Panel>
        </div>
        <Panel className="release-detail-panel">
          {selectedDraft ? <DraftDetail draft={selectedDraft} siteName={t(siteNames.get(selectedDraft.site_id) || selectedDraft.site_id)} onPublish={() => setPublishTarget(selectedDraft)} /> : selectedRelease ? <ReleaseDetail release={selectedRelease} detail={releaseDetail.data} siteName={t(siteNames.get(selectedRelease.site_id) || selectedRelease.site_id)} nodeLabels={nodeLabels} loading={releaseDetail.isLoading} error={releaseDetail.error} /> : releaseId ? <LoadingState rows={7} /> : <EmptyState title="Select a draft or release" detail="Its diff and node reconciliation appear here." />}
        </Panel>
      </section>
      <ConfirmDialog open={Boolean(publishTarget)} onOpenChange={(open) => !open && setPublishTarget(null)} title="Publish configuration draft" description={publishTarget ? t("Create a desired release for {site}. Nodes apply it independently and may still reject or roll back the change.", { site: t(siteNames.get(publishTarget.site_id) || publishTarget.site_id) }) : ""} confirmLabel="Publish release" busy={publish.isPending} onConfirm={() => publishTarget && publish.mutate(publishTarget)} />
    </div>
  );
}

function cnReleaseRow(selected: boolean) {
  return `release-row ${selected ? "release-row-selected" : ""}`;
}

function DraftDetail({ draft, siteName, onPublish }: { draft: Draft; siteName: string; onPublish: () => void }) {
  const { t, formatDate, formatNumber } = usePreferences();
  return <div className="release-detail"><div className="release-detail-heading"><div><span className="panel-kicker">{t("DRAFT")}</span><h2>{siteName}</h2><p>{t("Source revision v{revision} · expires {date}", { revision: formatNumber(draft.source_revision), date: formatDate(draft.expires_at) })}</p></div><Button variant="primary" onClick={onPublish}><Play size={16} />{t("Publish")}</Button></div><div className="detail-summary-grid"><div><span>{t("Risk")}</span><StatusBadge status={draft.risk_level} /></div><div><span>{t("Validation")}</span><StatusBadge status={draft.validation.valid === false ? "failed" : "valid"} /></div><div><span>{t("Targets")}</span><strong>{t("{count} nodes", { count: formatNumber(draft.node_ids.length) })}</strong></div></div><div className="diff-section"><div><span className="panel-kicker">{t("STRUCTURED DIFF")}</span><FileDiff size={17} /></div><pre className="diff-view">{JSON.stringify(draft.diff, null, 2)}</pre></div><div className="validation-list"><strong>{t("Effective source CIDRs")}</strong><code>{(draft.validation.effective_cidrs || []).join("\n") || t("No CIDRs supplied")}</code></div></div>;
}

const stageDescriptionKeys: Record<string, string> = {
  draft: "Desired state created",
  validated: "Bundle and policy checks passed",
  queued: "Waiting for the monitor",
  applying: "Monitor is applying the bundle",
  health_check: "Verifying services and firewall",
  succeeded: "All targeted nodes acknowledged",
};

function ReleaseDetail({ release, detail, siteName, nodeLabels, loading, error }: { release: Release; detail?: ReleaseDetailData; siteName: string; nodeLabels: Map<string, string>; loading: boolean; error: Error | null }) {
  const { t, formatDate, formatNumber, formatPercent, formatDuration } = usePreferences();
  const { toast } = useToast();
  const [activeTab, setActiveTab] = useState<"nodes" | "trace" | "diff">("nodes");
  const [copied, setCopied] = useState("");
  const data = detail;
  const ackItems = data?.acknowledgements || [];
  const position = release.status === "failed" ? releaseStages.length - 2 : stagePosition(release.stage);
  const desiredVersion = ackItems.find((item) => Number.isFinite(item.desired_version))?.desired_version;
  const elapsedMs = release.started_at ? Math.max(0, new Date(release.finished_at || Date.now()).getTime() - new Date(release.started_at).getTime()) : null;
  const ackRate = release.node_ids.length ? Math.round((ackItems.length / release.node_ids.length) * 100) : 0;
  const copy = async (value: string, label: string) => { try { await navigator.clipboard.writeText(value); setCopied(label); window.setTimeout(() => setCopied(""), 1_500); toast({ title: t("Copied"), variant: "success" }); } catch (copyError) { toast({ title: t("Operation failed"), description: copyError instanceof Error ? copyError.message : t("Unable to copy."), variant: "destructive" }); } };
  if (loading && !data) return <LoadingState rows={7} />;
  if (error && !data) return <ErrorState error={error.message} />;
  return <div className="release-workbench-detail">
    <header className="release-workbench-header"><div className="release-workbench-title"><div><span className="panel-kicker">{t("RELEASE")}</span><div className="release-title-line"><h2>{siteName}</h2><StatusBadge status={release.status === "succeeded" ? "published" : release.status} /></div><p><span>{t("Version flow")}: <strong>v{formatNumber(Math.max(0, (desiredVersion || 0) - 1))} → v{formatNumber(desiredVersion || 0)}</strong></span><span>{t("Elapsed")}: {elapsedMs === null ? "-" : formatDuration(elapsedMs)}</span><span>{t("Release ID")}: <code>{shortHash(release.release_id, 16)}</code></span></p></div></div><div className="release-workbench-actions"><Button size="sm" onClick={() => toast({ title: t("Diff view"), description: t("The structured configuration diff is available from the draft workspace.") })}><FileDiff size={14} />{t("Config diff")}</Button><Button size="sm" onClick={() => toast({ title: t("Republish"), description: t("Republish from the original draft to preserve release history.") })}><RotateCcw size={14} />{t("Republish")}</Button><Button size="sm" variant="primary" onClick={() => toast({ title: t("Rollback history"), description: t("Choose a previous release from the release history list.") })}><RotateCcw size={14} />{t("Rollback history")}</Button></div></header>
    <section className="release-kpi-strip"><div><span>{t("ACK rate")}</span><strong>{ackItems.length} / {release.node_ids.length} <em>{ackRate}% ACK</em></strong><small>{t("Target nodes acknowledged")}</small></div><div><span>{t("Target bundle hash")}</span><strong className="mono release-kpi-hash">{shortHash(ackItems[0]?.bundle_hash || release.desired_release_id, 16)}</strong><small>{t("Desired release bundle")}</small></div><div><span>{t("Core component status")}</span><strong className={release.status === "succeeded" ? "release-kpi-good" : ""}>{release.status === "succeeded" ? t("Healthy") : t(release.stage.replaceAll("_", " "))}</strong><small>sing-box · nftables · {t("health probe")}</small></div><div><span>{t("Completed at")}</span><strong>{release.finished_at ? formatDate(release.finished_at, true) : t("In progress")}</strong><small>{release.finished_at ? t("Release completed") : t("Awaiting final ACK")}</small></div></section>
    <ol className="release-workbench-pipeline">{releaseStages.map((stage, index) => { const complete = release.status === "succeeded" ? index <= position : index < position; const current = !complete && index === position; return <li className={`${complete ? "stage-complete" : ""} ${current ? "stage-current" : ""}`} key={stage}><span>{complete ? <Check size={15} /> : index + 1}</span><strong>{t(stage.replaceAll("_", " "))}</strong><small>{t(stageDescriptionKeys[stage])}</small></li>; })}</ol>
    <section className="release-workbench-main"><nav className="release-workbench-tabs" role="tablist"><button className={activeTab === "nodes" ? "active" : ""} onClick={() => setActiveTab("nodes")}>{t("Nodes and probes")} <b>{release.node_ids.length}</b></button><button className={activeTab === "trace" ? "active" : ""} onClick={() => setActiveTab("trace")}>{t("Full trace")}</button><button className={activeTab === "diff" ? "active" : ""} onClick={() => setActiveTab("diff")}>{t("Config diff")}</button><span>{t("Live coordination")}</span></nav>{activeTab === "nodes" ? <><div className="release-node-table"><div className="release-node-table-head"><span>{t("Target node")}</span><span>{t("Version state")}</span><span>{t("Component checks")}</span><span>{t("ACK")}</span><span>{t("Elapsed")}</span></div>{release.node_ids.map((nodeId) => { const ack = ackItems.find((item) => item.node_id === nodeId); const outcome = ack ? (ack.ok && ack.health_ok ? "succeeded" : "failed") : "pending"; return <div className="release-node-table-row" key={nodeId}><div className="release-node-name"><Server size={15} /><strong>{nodeLabels.get(nodeId) || nodeId}</strong><small className="mono">{nodeId}</small></div><div><strong>{ack ? `v${ack.applied_version}` : `v${desiredVersion || "-"}`}</strong><small>{ack ? t("Applied") : t("Desired")}</small></div><div className="release-node-checks"><StatusBadge status={ack ? ack.singbox_ok ? "valid" : "failed" : "pending"} /><StatusBadge status={ack ? ack.nft_ok ? "valid" : "failed" : "pending"} /><StatusBadge status={ack ? ack.health_ok ? "healthy" : "failed" : "pending"} /></div><StatusBadge status={outcome} /><span>{ack ? formatDate(ack.received_at, true) : "-"}</span></div>; })}</div><div className="release-terminal"><header><span>{t("Node execution and probe trace")}</span><StatusBadge status={release.status === "succeeded" ? "archived" : "running"} /></header><pre>{(data?.events || []).map((event) => `[${formatDate(event.timestamp, true)}] [${event.source}] ${event.message}`).join("\n") || t("Waiting for execution events...")}</pre></div></> : activeTab === "trace" ? <div className="release-trace-panel">{(data?.events || []).map((event, index) => <div className={`release-trace-event release-trace-${event.level}`} key={`${event.timestamp}-${event.source}-${index}`}><time>{formatDate(event.timestamp, true)}</time><strong>{event.source}</strong><span>{event.message}</span></div>)}</div> : <pre className="release-diff-panel">{JSON.stringify(data?.task?.result || { desired_release_id: release.desired_release_id, previous_release_id: release.previous_release_id }, null, 2)}</pre>}</section>
    <footer className="release-workbench-footer"><span>{t("Release ID")} <code>{shortHash(release.release_id, 18)}</code><button onClick={() => void copy(release.release_id, "release")}>{copied === "release" ? <Check size={13} /> : <Clipboard size={13} />}</button></span><span>{t("Task ID")} <code>{shortHash(release.task_id || "-", 18)}</code><button onClick={() => void copy(release.task_id || "", "task")}>{copied === "task" ? <Check size={13} /> : <Clipboard size={13} />}</button></span><span>{t("Baseline")}: <code>{shortHash(release.previous_release_id || "-", 14)}</code></span><span>{t("Coordination")}: <strong className="release-kpi-good">{release.status === "succeeded" ? t("Converged") : t(release.status)}</strong></span><span>{t("Trigger")}: {data?.task?.task_type || t("Manual")}</span></footer>
  </div>;
}
