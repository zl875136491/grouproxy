"use client";

import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { CalendarRange, CheckCircle2, CircleAlert, FileCheck2, FileDiff, Filter, MapPinned, Play } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import {
  getDrafts,
  getReleaseAcks,
  getReleases,
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
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  const siteNames = useMemo(() => new Map((sites.data || []).map((site) => [site.id, site.name])), [sites.data]);
  const draftItems = drafts.data || [];
  const releaseItems = releases.data || [];
  const selectedDraft = useMemo(() => draftItems.find((item) => item.id === draftId) || null, [draftId, draftItems]);
  const selectedRelease = useMemo(() => releaseItems.find((item) => item.release_id === releaseId) || (draftId ? null : releaseItems[0] || null), [draftId, releaseId, releaseItems]);
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

  const openDrafts = draftItems.filter((draft) => draft.status === "draft" && (!siteFilter || draft.site_id === siteFilter));
  const error = publish.error;
  return (
    <div className="page-stack releases-page">
      <section className="release-hero">
        <div><span className="release-hero-eyebrow">{t("DEPLOY")}</span><h1>{t("Recent releases")}</h1><p>{t("Review desired state, node ACKs, and the path from draft to applied configuration.")}</p></div>
        <div className="release-hero-metric"><FileCheck2 size={20} /><strong>{formatNumber(releaseItems.length)}</strong><span>{t("{count} releases", { count: formatNumber(releaseItems.length) })}</span></div>
      </section>
      {error ? <div className="inline-error" role="alert">{error instanceof Error ? error.message : t("The release could not be created.")}</div> : null}
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
              <div><span className="panel-kicker">{t(listView === "drafts" ? "READY TO PUBLISH" : "HISTORY")}</span><h2>{t(listView === "drafts" ? "Drafts" : "Release history")}</h2></div>
              <div className="release-list-actions">
                <div className="segmented-control" role="tablist" aria-label={t("Releases")}>
                  <button type="button" role="tab" aria-selected={listView === "drafts"} className={listView === "drafts" ? "segmented-active" : ""} onClick={() => setListView("drafts")}>{t("Drafts")}</button>
                  <button type="button" role="tab" aria-selected={listView === "history"} className={listView === "history" ? "segmented-active" : ""} onClick={() => setListView("history")}>{t("Release history")}</button>
                </div>
                <span className="count-label">{formatNumber(listView === "drafts" ? openDrafts.length : releaseItems.length)}</span>
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
          {selectedDraft ? <DraftDetail draft={selectedDraft} siteName={t(siteNames.get(selectedDraft.site_id) || selectedDraft.site_id)} onPublish={() => setPublishTarget(selectedDraft)} /> : selectedRelease ? <ReleaseDetail release={selectedRelease} siteName={t(siteNames.get(selectedRelease.site_id) || selectedRelease.site_id)} acknowledgements={acknowledgements} /> : <EmptyState title="Select a draft or release" detail="Its diff and node reconciliation appear here." />}
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

function ReleaseDetail({ release, siteName, acknowledgements }: { release: Release; siteName: string; acknowledgements: UseQueryResult<AgentAck[], Error> }) {
  const { t, formatDate, formatNumber, formatPercent } = usePreferences();
  const ackItems = acknowledgements.data || [];
  const ackByNode = new Map(ackItems.map((item) => [item.node_id, item]));
  const position = stagePosition(release.stage);
  return <div className="release-detail"><div className="release-detail-heading"><div><span className="panel-kicker">{t("RELEASE")}</span><h2>{siteName}</h2><p>{formatDate(release.created_at)} · {release.release_id.slice(0, 12)}</p></div><StatusBadge status={release.status} /></div><ol className="release-stages">{releaseStages.map((stage, index) => <li className={index <= position ? "stage-complete" : ""} key={stage}><span>{index < position || release.stage === "succeeded" ? <CheckCircle2 size={15} /> : index === position ? <CircleAlert size={15} /> : index + 1}</span><strong>{t(stage.replaceAll("_", " "))}</strong></li>)}</ol>{release.status === "failed" ? <div className="release-failure"><CircleAlert size={18} /><div><strong>{t("Release did not complete")}</strong><span>{t(release.rollback_reason || release.error || "One or more nodes returned a failed ACK.")}</span></div></div> : null}<div className="ack-section"><div className="panel-heading"><div><span className="panel-kicker">{t("NODE RECONCILIATION")}</span><h3>{t("ACK status")}</h3></div><span>{formatNumber(ackItems.length)} / {formatNumber(release.node_ids.length)}</span></div>{acknowledgements.isLoading ? <LoadingState rows={3} /> : acknowledgements.isError ? <ErrorState error="ACK data is unavailable." onRetry={() => void acknowledgements.refetch()} /> : <div className="ack-table">{release.node_ids.map((nodeId) => { const ack = ackByNode.get(nodeId); return <div className="ack-row" key={nodeId}><div><strong>{nodeId}</strong><span>{ack ? `${t(ack.stage.replaceAll("_", " "))} · v${formatNumber(ack.applied_version)}` : t("Awaiting node ACK")}</span></div><div className="ack-checks"><StatusBadge status={ack?.singbox_ok ? "valid" : ack ? "failed" : "pending"} /><StatusBadge status={ack?.nft_ok ? "valid" : ack ? "failed" : "pending"} /><StatusBadge status={ack?.health_ok ? "healthy" : ack ? "failed" : "pending"} /></div><StatusBadge status={ack ? ack.ok ? "succeeded" : ack.rollback_ok ? "rolled_back" : "failed" : "pending"} /></div>; })}</div>}</div><div className="release-meta"><div><span>{t("Task")}</span><strong className="mono">{shortHash(release.task_id || "", 16)}</strong></div><div><span>{t("Progress")}</span><strong>{formatPercent(release.progress)}</strong></div><div><span>{t("Previous release")}</span><strong className="mono">{shortHash(release.previous_release_id || "", 12)}</strong></div></div></div>;
}
