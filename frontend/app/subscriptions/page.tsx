"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CircleAlert,
  CirclePlus,
  FileUp,
  Play,
  RotateCcw,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  createSubscriptionSource,
  getNodes,
  getSites,
  getSubscriptions,
  getTask,
  publishSubscriptionVersion,
  refreshSubscription,
  rollbackSiteSubscription,
  uploadSubscription,
  type SiteSubscription,
  type SubscriptionVersion,
  type Task,
} from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { shortHash } from "../../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, IconButton, Panel, RefreshButton, StatusBadge } from "../../components/ui";

type FormMode = "source" | "upload" | null;

function isFinishedRefreshTask(task: Task) {
  return ["succeeded", "failed", "cancelled", "dead_letter"].includes(task.status);
}

async function waitForRefreshTask(taskId: string): Promise<Task> {
  while (true) {
    const task = await getTask(taskId);
    if (isFinishedRefreshTask(task)) {
      if (task.status !== "succeeded") throw new Error(task.error || "subscription_refresh_failed");
      return task;
    }
    await new Promise<void>((resolve) => window.setTimeout(resolve, 1_000));
  }
}

export default function SubscriptionsPage() {
  const { t, formatBytes, formatDate, formatNumber } = usePreferences();
  const session = useManagementSession();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [formMode, setFormMode] = useState<FormMode>(null);
  const [sourceName, setSourceName] = useState("");
  const [sourceURL, setSourceURL] = useState("");
  const [fetchInterval, setFetchInterval] = useState("21600");
  const [uploadName, setUploadName] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [selectedSourceId, setSelectedSourceId] = useState("");
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [selectedSiteIds, setSelectedSiteIds] = useState<string[]>([]);
  const [siteSelectionReady, setSiteSelectionReady] = useState(false);
  const [publishTarget, setPublishTarget] = useState<SubscriptionVersion | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState<SiteSubscription | null>(null);

  const catalog = useQuery({
    queryKey: ["subscriptions"],
    queryFn: getSubscriptions,
    enabled: session === true,
    refetchInterval: 5_000,
  });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true });
  const sourceCreate = useMutation({
    mutationFn: () => createSubscriptionSource({
      name: sourceName.trim(),
      url: sourceURL.trim(),
      fetch_interval_sec: Number(fetchInterval),
    }),
    onSuccess: async (result) => {
      setSourceName("");
      setSourceURL("");
      setFormMode(null);
      setSelectedSourceId(result.source.id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["subscriptions"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      ]);
    },
  });
  const upload = useMutation({
    mutationFn: () => uploadSubscription(uploadName.trim(), uploadFile!),
    onSuccess: async (result) => {
      setUploadName("");
      setUploadFile(null);
      setFormMode(null);
      setSelectedSourceId(result.source.id);
      setSelectedVersionId(result.version.id);
      await queryClient.invalidateQueries({ queryKey: ["subscriptions"] });
    },
  });
  const refresh = useMutation({
    mutationFn: (sourceId: string) => refreshSubscription(sourceId),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["subscriptions"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      ]);
    },
  });
  const publish = useMutation({
    mutationFn: (version: SubscriptionVersion) => publishSubscriptionVersion(
      version.source_id,
      version.id,
      selectedSiteIds,
    ),
    onSuccess: async (result) => {
      setPublishTarget(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["subscriptions"] }),
        queryClient.invalidateQueries({ queryKey: ["releases"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
        queryClient.invalidateQueries({ queryKey: ["nodes"] }),
      ]);
      if (result.releases[0]) router.push(`/releases?release=${result.releases[0].release_id}`);
    },
  });
  const rollback = useMutation({
    mutationFn: (siteId: string) => rollbackSiteSubscription(siteId),
    onSuccess: async (result) => {
      setRollbackTarget(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["subscriptions"] }),
        queryClient.invalidateQueries({ queryKey: ["releases"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      ]);
      if (result.releases[0]) router.push(`/releases?release=${result.releases[0].release_id}`);
    },
  });

  const sourceItems = catalog.data?.sources || [];
  const versionItems = catalog.data?.versions || [];
  const siteItems = sites.data || [];
  const nodeItems = nodes.data || [];
  const deployableSiteIds = useMemo(
    () => [...new Set(nodeItems.map((node) => node.site_id))],
    [nodeItems],
  );
  const currentSource = sourceItems.find((item) => item.id === selectedSourceId) || sourceItems[0] || null;
  const currentVersions = currentSource
    ? versionItems.filter((item) => item.source_id === currentSource.id)
    : [];
  const currentVersion = currentVersions.find((item) => item.id === selectedVersionId)
    || currentVersions[0]
    || null;
  const versionById = useMemo(() => new Map(versionItems.map((item) => [item.id, item])), [versionItems]);
  const sourceById = useMemo(() => new Map(sourceItems.map((item) => [item.id, item])), [sourceItems]);
  const siteById = useMemo(() => new Map(siteItems.map((item) => [item.id, item])), [siteItems]);

  useEffect(() => {
    if (!selectedSourceId && sourceItems[0]) setSelectedSourceId(sourceItems[0].id);
  }, [selectedSourceId, sourceItems]);

  useEffect(() => {
    if (!currentVersion) return;
    if (!selectedVersionId || !currentVersions.some((item) => item.id === selectedVersionId)) {
      setSelectedVersionId(currentVersion.id);
    }
  }, [currentVersion, currentVersions, selectedVersionId]);

  useEffect(() => {
    if (!siteSelectionReady && deployableSiteIds.length) {
      setSelectedSiteIds(deployableSiteIds);
      setSiteSelectionReady(true);
    }
  }, [deployableSiteIds, siteSelectionReady]);

  function submitSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sourceName.trim() && sourceURL.trim()) sourceCreate.mutate();
  }

  function submitUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (uploadName.trim() && uploadFile) upload.mutate();
  }

  function toggleSite(siteId: string) {
    setSelectedSiteIds((current) => (
      current.includes(siteId) ? current.filter((item) => item !== siteId) : [...current, siteId]
    ));
  }

  if (session === null) return <LoadingState rows={8} />;
  if (!session) return <SessionGate />;
  if (catalog.isLoading || sites.isLoading || nodes.isLoading) return <LoadingState rows={8} />;
  if (catalog.isError || sites.isError || nodes.isError) {
    const error = catalog.error || sites.error || nodes.error;
    return <ErrorState error={error instanceof Error ? error.message : "Unable to load subscription state."} onRetry={() => void Promise.all([catalog.refetch(), sites.refetch(), nodes.refetch()])} />;
  }

  const mutationError = sourceCreate.error || upload.error || refresh.error || publish.error || rollback.error;
  const activeBindings = catalog.data?.site_subscriptions || [];

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="DEPLOY"
        title="Subscriptions"
        description="Versioned upstream content is selected per site and released through the normal node ACK path."
        actions={<><Button onClick={() => setFormMode(formMode === "upload" ? null : "upload")}><FileUp size={16} /> {t("Upload file")}</Button><Button variant="primary" onClick={() => setFormMode(formMode === "source" ? null : "source")}><CirclePlus size={16} /> {t("Add HTTP source")}</Button></>}
      />
      {mutationError ? <div className="inline-error" role="alert">{mutationError instanceof Error ? mutationError.message : "The subscription operation was not accepted."}</div> : null}
      {formMode === "source" ? <Panel><form className="subscription-form" onSubmit={submitSource}><label><span>{t("Name")}</span><input autoFocus value={sourceName} onChange={(event) => setSourceName(event.target.value)} placeholder={t("Regional upstream")} /></label><label><span>{t("HTTP URL")}</span><input type="url" value={sourceURL} onChange={(event) => setSourceURL(event.target.value)} placeholder="http://upstream.example/subscription" /></label><label><span>{t("Refresh interval")}</span><select value={fetchInterval} onChange={(event) => setFetchInterval(event.target.value)}><option value="3600">{t("1 hour")}</option><option value="21600">{t("6 hours")}</option><option value="86400">{t("24 hours")}</option></select></label><div className="form-actions"><Button type="button" onClick={() => setFormMode(null)}>{t("Cancel")}</Button><Button variant="primary" type="submit" disabled={sourceCreate.isPending}>{sourceCreate.isPending ? t("Queueing...") : t("Queue refresh")}</Button></div></form></Panel> : null}
      {formMode === "upload" ? <Panel><form className="subscription-form subscription-upload-form" onSubmit={submitUpload}><label><span>{t("Name")}</span><input autoFocus value={uploadName} onChange={(event) => setUploadName(event.target.value)} placeholder={t("Imported upstream")} /></label><label><span>{t("Subscription file")}</span><input type="file" accept=".json,.yaml,.yml,application/json,application/x-yaml,text/yaml" onChange={(event) => setUploadFile(event.target.files?.[0] || null)} /></label><div className="form-actions"><Button type="button" onClick={() => setFormMode(null)}>{t("Cancel")}</Button><Button variant="primary" type="submit" disabled={!uploadFile || upload.isPending}>{upload.isPending ? t("Importing...") : t("Import version")}</Button></div></form></Panel> : null}
      <section className="subscription-layout">
        <Panel>
          <div className="panel-heading"><div><span className="panel-kicker">{t("UPSTREAMS")}</span><h2>{t("Sources")}</h2></div><span className="count-label">{formatNumber(sourceItems.length)}</span></div>
          {sourceItems.length ? <div className="subscription-source-list">{sourceItems.map((source) => <div className={`subscription-source-row ${currentSource?.id === source.id ? "subscription-source-selected" : ""}`} key={source.id}><button onClick={() => { setSelectedSourceId(source.id); setSelectedVersionId(""); }}><span><strong>{source.name}</strong><small>{source.url_hint} · {source.last_refresh_at ? t("Refreshed {date}", { date: formatDate(source.last_refresh_at) }) : t("Not refreshed")}</small></span><StatusBadge status={source.last_refresh_error ? "failed" : source.last_refresh_at ? "current" : "pending"} /></button>{source.refreshable ? <RefreshButton label={t("Refresh {name}", { name: source.name })} disabled={refresh.isPending} onRefresh={async () => { const result = await refresh.mutateAsync(source.id); return waitForRefreshTask(result.task.task_id); }} /> : null}</div>)}</div> : <EmptyState title="No subscription sources" detail="Add an HTTP source or import a supported file." />}
        </Panel>
        <Panel className="subscription-detail-panel">
          {currentVersion ? <VersionDetail version={currentVersion} siteItems={siteItems} deployableSiteIds={deployableSiteIds} selectedSiteIds={selectedSiteIds} onToggleSite={toggleSite} onPublish={() => setPublishTarget(currentVersion)} /> : <EmptyState title="Select a source version" detail="Parsed versions are available after a refresh or file import." />}
        </Panel>
      </section>
      <Panel>
        <div className="panel-heading"><div><span className="panel-kicker">{t("IMMUTABLE HISTORY")}</span><h2>{t("Versions")}</h2></div><span className="count-label">{formatNumber(currentVersions.length)}</span></div>
          {currentVersions.length ? <div className="table-wrap"><table><thead><tr><th>{t("Version")}</th><th>{t("Hash")}</th><th>{t("Format")}</th><th>{t("Nodes")}</th><th>{t("Parsed")}</th><th>{t("Fetched")}</th><th>{t("State")}</th></tr></thead><tbody>{currentVersions.map((version) => <tr className={currentVersion?.id === version.id ? "subscription-version-selected" : ""} key={version.id} onClick={() => setSelectedVersionId(version.id)}><td><strong>v{formatNumber(version.version)}</strong><span className="cell-secondary">{formatBytes(version.size_bytes)}</span></td><td className="mono" title={version.content_hash}>{shortHash(version.content_hash, 18)}</td><td><span className="type-tag">{version.format}</span></td><td>{formatNumber(version.node_count)}</td><td><StatusBadge status={version.parse_ok ? "valid" : "failed"} /></td><td>{formatDate(version.fetched_at)}</td><td><StatusBadge status={version.published ? "published" : "ready"} /></td></tr>)}</tbody></table></div> : <EmptyState title="No versions for this source" detail="Refresh or import content to create an immutable version." />}
      </Panel>
      <Panel>
        <div className="panel-heading"><div><span className="panel-kicker">{t("SITE SELECTIONS")}</span><h2>{t("Active deployments")}</h2></div><span className="count-label">{formatNumber(activeBindings.length)}</span></div>
        {activeBindings.length ? <div className="table-wrap"><table><thead><tr><th>{t("Site")}</th><th>{t("Source")}</th><th>{t("Current version")}</th><th>{t("Previous version")}</th><th>{t("Updated")}</th><th aria-label={t("Actions")} /></tr></thead><tbody>{activeBindings.map((binding) => <tr key={binding.site_id}><td><strong>{t(siteById.get(binding.site_id)?.name || binding.site_id)}</strong></td><td>{sourceById.get(binding.source_id)?.name || binding.source_id}</td><td className="mono">{versionLabel(versionById.get(binding.subscription_version_id), formatNumber)}</td><td className="mono">{versionLabel(binding.previous_subscription_version_id ? versionById.get(binding.previous_subscription_version_id) : undefined, formatNumber)}</td><td>{formatDate(binding.updated_at)}</td><td>{binding.previous_subscription_version_id ? <IconButton label={t("Roll back {name}", { name: t(siteById.get(binding.site_id)?.name || binding.site_id) })} disabled={rollback.isPending} onClick={() => setRollbackTarget(binding)}><RotateCcw size={16} /></IconButton> : null}</td></tr>)}</tbody></table></div> : <EmptyState title="No sites have a selected subscription" />}
      </Panel>
      <ConfirmDialog open={Boolean(publishTarget)} onOpenChange={(open) => !open && setPublishTarget(null)} title="Publish subscription version" description={publishTarget ? t("Release {version} to {count} selected sites. Each edge node validates and acknowledges the new outbound configuration independently.", { version: versionLabel(publishTarget, formatNumber), count: formatNumber(selectedSiteIds.length) }) : ""} confirmLabel="Publish version" busy={publish.isPending} onConfirm={() => publishTarget && publish.mutate(publishTarget)} />
      <ConfirmDialog open={Boolean(rollbackTarget)} onOpenChange={(open) => !open && setRollbackTarget(null)} title="Roll back site subscription" description={rollbackTarget ? t("Restore the previous selected version for {site}. This creates a new release and waits for node ACKs.", { site: t(siteById.get(rollbackTarget.site_id)?.name || rollbackTarget.site_id) }) : ""} confirmLabel="Create rollback release" danger busy={rollback.isPending} onConfirm={() => rollbackTarget && rollback.mutate(rollbackTarget.site_id)} />
    </div>
  );
}

function VersionDetail({
  version,
  siteItems,
  deployableSiteIds,
  selectedSiteIds,
  onToggleSite,
  onPublish,
}: {
  version: SubscriptionVersion;
  siteItems: Array<{ id: string; name: string; slug: string }>;
  deployableSiteIds: string[];
  selectedSiteIds: string[];
  onToggleSite: (siteId: string) => void;
  onPublish: () => void;
}) {
  const { t, formatBytes, formatDate, formatNumber } = usePreferences();
  return (
    <div className="subscription-detail">
      <div className="release-detail-heading"><div><span className="panel-kicker">{t("SELECTED VERSION")}</span><h2>{versionLabel(version, formatNumber)}</h2><p>{version.format} · {formatBytes(version.size_bytes)} · {t("Fetched {date}", { date: formatDate(version.fetched_at) })}</p></div><StatusBadge status={version.parse_ok ? "valid" : "failed"} /></div>
      <div className="detail-summary-grid"><div><span>{t("Content hash")}</span><strong className="mono" title={version.content_hash}>{shortHash(version.content_hash, 18)}</strong></div><div><span>{t("Endpoint count")}</span><strong>{formatNumber(version.node_count)}</strong></div><div><span>{t("Publication")}</span><StatusBadge status={version.published ? "published" : "ready"} /></div></div>
      {version.parse_ok ? <div className="subscription-site-selection"><div className="subscription-selection-heading"><div><span className="panel-kicker">{t("DEPLOY TARGETS")}</span><strong>{t("Sites")}</strong></div><span>{t("{count} selected", { count: formatNumber(selectedSiteIds.length) })}</span></div><div className="subscription-site-checks">{siteItems.map((site) => { const deployable = deployableSiteIds.includes(site.id); return <label className={!deployable ? "subscription-site-disabled" : ""} key={site.id}><input type="checkbox" checked={selectedSiteIds.includes(site.id)} disabled={!deployable} onChange={() => onToggleSite(site.id)} /><span><strong>{t(site.name)}</strong><small>{deployable ? site.slug : t("No edge node")}</small></span></label>; })}</div><Button variant="primary" disabled={!selectedSiteIds.length} onClick={onPublish}><Play size={16} /> {t("Publish to selected sites")}</Button></div> : <div className="subscription-parse-error"><CircleAlert size={18} /><div><strong>{t("Version cannot be published")}</strong><span>{version.parse_error || t("Parsing did not produce usable outbounds.")}</span></div></div>}
    </div>
  );
}

function versionLabel(
  version: SubscriptionVersion | undefined,
  formatNumber: (value: number | null | undefined) => string = (value) => String(value ?? "-"),
) {
  return version ? `v${formatNumber(version.version)} · ${shortHash(version.content_hash, 10)}` : "-";
}
