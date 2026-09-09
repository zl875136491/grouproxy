"use client";

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { CalendarRange, CheckCircle2, ChevronDown, CircleAlert, FileCheck2, FileDiff, Filter, MapPinned, Play, RotateCcw, Server, ShieldCheck } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import {
  getDrafts,
  getRelease,
  getReleaseAcks,
  getReleases,
  getNodes,
  getSites,
  publishRelease,
  type AgentAck,
  type Draft,
  type Release,
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
  const acknowledgements = useQuery({ queryKey: ["release-acks", selectedRelease?.release_id], queryFn: () => getReleaseAcks(selectedRelease!.release_id), enabled: Boolean(selectedRelease), refetchInterval: selectedRelease?.status === "succeeded" || selectedRelease?.status === "failed" ? false : 2_000 });

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
          {selectedDraft ? <DraftDetail draft={selectedDraft} siteName={t(siteNames.get(selectedDraft.site_id) || selectedDraft.site_id)} onPublish={() => setPublishTarget(selectedDraft)} /> : selectedRelease ? <ReleaseDetail release={selectedRelease} siteName={t(siteNames.get(selectedRelease.site_id) || selectedRelease.site_id)} nodeLabels={nodeLabels} acknowledgements={acknowledgements} /> : releaseId ? <LoadingState rows={7} /> : <EmptyState title="Select a draft or release" detail="Its diff and node reconciliation appear here." />}
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

function AckCheck({ label, status }: { label: string; status: string }) {
  return <span className="ack-check"><span>{label}</span><StatusBadge status={status} /></span>;
}

function ReleaseDetail({ release, siteName, nodeLabels, acknowledgements }: { release: Release; siteName: string; nodeLabels: Map<string, string>; acknowledgements: UseQueryResult<AgentAck[], Error> }) {
  const { t, formatDate, formatNumber, formatPercent, formatDuration } = usePreferences();
  const { toast } = useToast();
  const failureToastKey = useRef("");
  const ackItems = acknowledgements.data || [];
  const ackByNode = new Map(ackItems.map((item) => [item.node_id, item]));
  // The API records terminal failures as `stage: failed` after all node ACKs
  // arrive. Keep the failure marker on the last operational checkpoint so it
  // does not misleadingly point back to the initial draft step.
  const position = release.status === "failed" ? releaseStages.length - 2 : stagePosition(release.stage);
  const desiredVersion = ackItems.find((item) => Number.isFinite(item.desired_version))?.desired_version;
  const elapsedMs = release.started_at
    ? Math.max(0, new Date(release.finished_at || Date.now()).getTime() - new Date(release.started_at).getTime())
    : null;
  const failedAcks = useMemo(
    () => ackItems.filter((item) => !item.ok || !item.health_ok),
    [ackItems],
  );

  useEffect(() => {
    // A failed release is terminal, but ACK polling can render it several
    // times. Wait for the final ACK response and identify the notification by
    // its release plus error details so a refresh never spams the operator.
    if (release.status !== "failed" || acknowledgements.isFetching || !acknowledgements.isFetched) return;
    const fallback = t(release.rollback_reason || release.error || "One or more nodes returned a failed ACK.");
    const details = failedAcks.slice(0, 3).map((ack) => {
      const code = ack.error_code || "one_or_more_nodes_failed";
      const message = ack.error_message && ack.error_message !== ack.error_code
        ? `: ${ack.error_message.slice(0, 240)}`
        : "";
      return `${ack.node_id}: ${t(code)}${message}`;
    });
    const description = details.join("; ") || fallback;
    const key = `${release.release_id}:${description}`;
    if (failureToastKey.current === key) return;
    failureToastKey.current = key;
    toast({
      title: t("Release did not complete"),
      description,
      variant: "destructive",
      duration: 10_000,
    });
  }, [acknowledgements.isFetched, acknowledgements.isFetching, failedAcks, release.error, release.release_id, release.rollback_reason, release.status, t, toast]);

  return (
    <div className="release-detail">
      <div className="release-detail-heading">
        <div>
          <span className="panel-kicker">{t("RELEASE")}</span>
          <h2>{siteName}</h2>
          <p>{formatDate(release.created_at)} · {t("Release ID")} <span className="mono">{shortHash(release.release_id, 16)}</span></p>
        </div>
        <StatusBadge status={release.status} />
      </div>

      <div className="release-summary-grid">
        <div>
          <span>{t("Target release")}</span>
          <strong className="mono">{shortHash(release.desired_release_id, 16)}</strong>
          {desiredVersion !== undefined ? <small>{t("Desired version v{version}", { version: formatNumber(desiredVersion) })}</small> : null}
        </div>
        <div>
          <span>{t("Target nodes")}</span>
          <strong>{t("{count} nodes", { count: formatNumber(release.node_ids.length) })}</strong>
          <small>{t("{count} acknowledged", { count: formatNumber(ackItems.length) })}</small>
        </div>
        <div>
          <span>{t("Progress")}</span>
          <strong>{formatPercent(release.progress)}</strong>
          <span className="release-progress"><span style={{ width: `${Math.max(0, Math.min(100, release.progress))}%` }} /></span>
        </div>
        <div>
          <span>{t("Elapsed")}</span>
          <strong>{elapsedMs === null ? "-" : formatDuration(elapsedMs)}</strong>
          <small>{release.finished_at ? t("Finished {date}", { date: formatDate(release.finished_at) }) : release.started_at ? t("Started {date}", { date: formatDate(release.started_at) }) : t("Not started")}</small>
        </div>
      </div>

      <ol className="release-stages">
        {releaseStages.map((stage, index) => {
          const complete = release.status === "succeeded" ? index <= position : index < position;
          const current = !complete && index === position;
          const failed = current && release.status === "failed";
          return <li className={`${complete ? "stage-complete" : ""} ${current ? "stage-current" : ""} ${failed ? "stage-failed" : ""}`} key={stage}>
            <span>{complete ? <CheckCircle2 size={15} /> : current ? <CircleAlert size={15} /> : index + 1}</span>
            <div><strong>{t(stage.replaceAll("_", " "))}</strong><small>{t(stageDescriptionKeys[stage])}</small></div>
          </li>;
        })}
      </ol>

      <div className="ack-section">
        <div className="panel-heading">
          <div><span className="panel-kicker">{t("NODE RECONCILIATION")}</span><h3>{t("ACK status")}</h3></div>
          <span className="ack-count">{formatNumber(ackItems.length)} / {formatNumber(release.node_ids.length)} {t("ACKs")}</span>
        </div>
        {acknowledgements.isLoading ? <LoadingState rows={3} /> : acknowledgements.isError ? <ErrorState error="ACK data is unavailable." onRetry={() => void acknowledgements.refetch()} /> : <div className="ack-table">
          {release.node_ids.map((nodeId) => {
            const ack = ackByNode.get(nodeId);
            const hashMatches = ack ? Boolean(ack.bundle_hash && ack.applied_hash && ack.bundle_hash === ack.applied_hash) : false;
            const outcome = ack ? (ack.ok ? "succeeded" : ack.rollback_ok ? "rolled_back" : "failed") : "pending";
            return <div className="ack-row" key={nodeId}>
              <div className="ack-row-main">
                <div className="ack-node-heading"><span className="ack-node-icon"><Server size={15} /></span><div><strong>{nodeLabels.get(nodeId) || nodeId}</strong>{nodeLabels.get(nodeId) && nodeLabels.get(nodeId) !== nodeId ? <small className="mono">{nodeId}</small> : null}</div></div>
                {ack ? <div className="ack-version-line"><span>{t("Desired v{version}", { version: formatNumber(ack.desired_version) })}</span><span>{t("Applied v{version}", { version: formatNumber(ack.applied_version) })}</span><span>{formatDate(ack.received_at)}</span></div> : <span className="ack-awaiting">{t("Awaiting node ACK")}</span>}
                {ack?.error_message ? <span className="ack-error">{t(ack.error_code || "Node reported an error")}: {ack.error_message}</span> : null}
              </div>
              <div className="ack-checks">
                <AckCheck label="sing-box" status={ack ? ack.singbox_ok ? "valid" : "failed" : "pending"} />
                <AckCheck label="nftables" status={ack ? ack.nft_ok ? "valid" : "failed" : "pending"} />
                <AckCheck label={t("Health")} status={ack ? ack.health_ok ? "healthy" : "failed" : "pending"} />
                <AckCheck label={t("Bundle")} status={ack ? hashMatches ? "valid" : "failed" : "pending"} />
              </div>
              <div className="ack-row-result"><StatusBadge status={outcome} />{ack?.rollback_attempted ? <span className="ack-rollback"><RotateCcw size={13} />{t(ack.rollback_ok ? "Rollback applied" : "Rollback failed")}</span> : null}</div>
            </div>;
          })}
        </div>}
      </div>

      <details className="release-coordination" open>
        <summary className="release-summary-toggle">
          <span className="release-summary-toggle-copy"><span className="panel-kicker">{t("COORDINATION")}</span><strong>{t("Release summary")}</strong><small>{t("Identifiers, timings, and node outcomes")}</small></span>
          <span className="release-summary-toggle-icon" aria-hidden="true"><ShieldCheck size={17} /><ChevronDown size={16} /></span>
        </summary>
        <div className="release-summary-body">
          <div className="release-meta">
            <div><span>{t("Release ID")}</span><strong className="mono">{release.release_id || "-"}</strong></div>
            <div><span>{t("Desired release")}</span><strong className="mono">{release.desired_release_id || "-"}</strong></div>
            <div><span>{t("Task")}</span><strong className="mono">{release.task_id || "-"}</strong></div>
            <div><span>{t("Previous release")}</span><strong className="mono">{release.previous_release_id || "-"}</strong></div>
            <div><span>{t("Created")}</span><strong>{formatDate(release.created_at)}</strong></div>
            <div><span>{t("Started")}</span><strong>{formatDate(release.started_at)}</strong></div>
            <div><span>{t("Finished")}</span><strong>{formatDate(release.finished_at)}</strong></div>
            <div><span>{t("Stage")}</span><strong>{t(release.stage.replaceAll("_", " "))}</strong></div>
          </div>
          {release.error || failedAcks.length ? <div className="release-failure-detail"><strong>{t("Failure details")}</strong>{release.error ? <p>{t(release.error)}</p> : null}{failedAcks.length ? <ul>{failedAcks.map((ack) => <li key={`${ack.node_id}-${ack.received_at}`}><span>{nodeLabels.get(ack.node_id) || ack.node_id}</span><code>{t(ack.error_code || "Node reported an error")}{ack.error_message && ack.error_message !== ack.error_code ? `: ${ack.error_message}` : ""}</code></li>)}</ul> : null}</div> : null}
        </div>
      </details>
    </div>
  );
}
