"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CircleAlert,
  CirclePlus,
  FileUp,
  Play,
  RefreshCw,
  RotateCcw,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  createSubscriptionSource,
  getNodes,
  getSites,
  getSubscriptions,
  publishSubscriptionVersion,
  refreshSubscription,
  rollbackSiteSubscription,
  uploadSubscription,
  type SiteSubscription,
  type SubscriptionVersion,
} from "../../lib/api";
import { formatDate, shortHash } from "../../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, IconButton, Panel, StatusBadge } from "../../components/ui";

type FormMode = "source" | "upload" | null;

export default function SubscriptionsPage() {
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
        actions={<><Button onClick={() => setFormMode(formMode === "upload" ? null : "upload")}><FileUp size={16} /> Upload file</Button><Button variant="primary" onClick={() => setFormMode(formMode === "source" ? null : "source")}><CirclePlus size={16} /> Add HTTP source</Button></>}
      />
      {mutationError ? <div className="inline-error" role="alert">{mutationError instanceof Error ? mutationError.message : "The subscription operation was not accepted."}</div> : null}
      {formMode === "source" ? <Panel><form className="subscription-form" onSubmit={submitSource}><label><span>Name</span><input autoFocus value={sourceName} onChange={(event) => setSourceName(event.target.value)} placeholder="Regional upstream" /></label><label><span>HTTP URL</span><input type="url" value={sourceURL} onChange={(event) => setSourceURL(event.target.value)} placeholder="http://upstream.example/subscription" /></label><label><span>Refresh interval</span><select value={fetchInterval} onChange={(event) => setFetchInterval(event.target.value)}><option value="3600">1 hour</option><option value="21600">6 hours</option><option value="86400">24 hours</option></select></label><div className="form-actions"><Button type="button" onClick={() => setFormMode(null)}>Cancel</Button><Button variant="primary" type="submit" disabled={sourceCreate.isPending}>{sourceCreate.isPending ? "Queueing..." : "Queue refresh"}</Button></div></form></Panel> : null}
      {formMode === "upload" ? <Panel><form className="subscription-form subscription-upload-form" onSubmit={submitUpload}><label><span>Name</span><input autoFocus value={uploadName} onChange={(event) => setUploadName(event.target.value)} placeholder="Imported upstream" /></label><label><span>Subscription file</span><input type="file" accept=".json,.yaml,.yml,application/json,application/x-yaml,text/yaml" onChange={(event) => setUploadFile(event.target.files?.[0] || null)} /></label><div className="form-actions"><Button type="button" onClick={() => setFormMode(null)}>Cancel</Button><Button variant="primary" type="submit" disabled={!uploadFile || upload.isPending}>{upload.isPending ? "Importing..." : "Import version"}</Button></div></form></Panel> : null}
      <section className="subscription-layout">
        <Panel>
          <div className="panel-heading"><div><span className="panel-kicker">UPSTREAMS</span><h2>Sources</h2></div><span className="count-label">{sourceItems.length}</span></div>
          {sourceItems.length ? <div className="subscription-source-list">{sourceItems.map((source) => <div className={`subscription-source-row ${currentSource?.id === source.id ? "subscription-source-selected" : ""}`} key={source.id}><button onClick={() => { setSelectedSourceId(source.id); setSelectedVersionId(""); }}><span><strong>{source.name}</strong><small>{source.url_hint} · {source.last_refresh_at ? `refreshed ${formatDate(source.last_refresh_at)}` : "not refreshed"}</small></span><StatusBadge status={source.last_refresh_error ? "failed" : source.last_refresh_at ? "current" : "pending"} /></button>{source.refreshable ? <IconButton label={`Refresh ${source.name}`} disabled={refresh.isPending} onClick={() => refresh.mutate(source.id)}><RefreshCw size={16} /></IconButton> : null}</div>)}</div> : <EmptyState title="No subscription sources" detail="Add an HTTP source or import a supported file." />}
        </Panel>
        <Panel className="subscription-detail-panel">
          {currentVersion ? <VersionDetail version={currentVersion} siteItems={siteItems} deployableSiteIds={deployableSiteIds} selectedSiteIds={selectedSiteIds} onToggleSite={toggleSite} onPublish={() => setPublishTarget(currentVersion)} /> : <EmptyState title="Select a source version" detail="Parsed versions are available after a refresh or file import." />}
        </Panel>
      </section>
      <Panel>
        <div className="panel-heading"><div><span className="panel-kicker">IMMUTABLE HISTORY</span><h2>Versions</h2></div><span className="count-label">{currentVersions.length}</span></div>
        {currentVersions.length ? <div className="table-wrap"><table><thead><tr><th>Version</th><th>Hash</th><th>Format</th><th>Nodes</th><th>Parsed</th><th>Fetched</th><th>State</th></tr></thead><tbody>{currentVersions.map((version) => <tr className={currentVersion?.id === version.id ? "subscription-version-selected" : ""} key={version.id} onClick={() => setSelectedVersionId(version.id)}><td><strong>v{version.version}</strong><span className="cell-secondary">{version.size_bytes.toLocaleString()} bytes</span></td><td className="mono" title={version.content_hash}>{shortHash(version.content_hash, 18)}</td><td><span className="type-tag">{version.format}</span></td><td>{version.node_count}</td><td><StatusBadge status={version.parse_ok ? "valid" : "failed"} /></td><td>{formatDate(version.fetched_at)}</td><td><StatusBadge status={version.published ? "published" : "ready"} /></td></tr>)}</tbody></table></div> : <EmptyState title="No versions for this source" detail="Refresh or import content to create an immutable version." />}
      </Panel>
      <Panel>
        <div className="panel-heading"><div><span className="panel-kicker">SITE SELECTIONS</span><h2>Active deployments</h2></div><span className="count-label">{activeBindings.length}</span></div>
        {activeBindings.length ? <div className="table-wrap"><table><thead><tr><th>Site</th><th>Source</th><th>Current version</th><th>Previous version</th><th>Updated</th><th aria-label="Actions" /></tr></thead><tbody>{activeBindings.map((binding) => <tr key={binding.site_id}><td><strong>{siteById.get(binding.site_id)?.name || binding.site_id}</strong></td><td>{sourceById.get(binding.source_id)?.name || binding.source_id}</td><td className="mono">{versionLabel(versionById.get(binding.subscription_version_id))}</td><td className="mono">{versionLabel(binding.previous_subscription_version_id ? versionById.get(binding.previous_subscription_version_id) : undefined)}</td><td>{formatDate(binding.updated_at)}</td><td>{binding.previous_subscription_version_id ? <IconButton label={`Roll back ${siteById.get(binding.site_id)?.name || binding.site_id}`} disabled={rollback.isPending} onClick={() => setRollbackTarget(binding)}><RotateCcw size={16} /></IconButton> : null}</td></tr>)}</tbody></table></div> : <EmptyState title="No sites have a selected subscription" />}
      </Panel>
      <ConfirmDialog open={Boolean(publishTarget)} onOpenChange={(open) => !open && setPublishTarget(null)} title="Publish subscription version" description={publishTarget ? `Release ${versionLabel(publishTarget)} to ${selectedSiteIds.length} selected site${selectedSiteIds.length === 1 ? "" : "s"}. Each edge node validates and acknowledges the new outbound configuration independently.` : ""} confirmLabel="Publish version" busy={publish.isPending} onConfirm={() => publishTarget && publish.mutate(publishTarget)} />
      <ConfirmDialog open={Boolean(rollbackTarget)} onOpenChange={(open) => !open && setRollbackTarget(null)} title="Roll back site subscription" description={rollbackTarget ? `Restore the previous selected version for ${siteById.get(rollbackTarget.site_id)?.name || rollbackTarget.site_id}. This creates a new release and waits for node ACKs.` : ""} confirmLabel="Create rollback release" danger busy={rollback.isPending} onConfirm={() => rollbackTarget && rollback.mutate(rollbackTarget.site_id)} />
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
  return (
    <div className="subscription-detail">
      <div className="release-detail-heading"><div><span className="panel-kicker">SELECTED VERSION</span><h2>{versionLabel(version)}</h2><p>{version.format} · {version.size_bytes.toLocaleString()} bytes · fetched {formatDate(version.fetched_at)}</p></div><StatusBadge status={version.parse_ok ? "valid" : "failed"} /></div>
      <div className="detail-summary-grid"><div><span>Content hash</span><strong className="mono" title={version.content_hash}>{shortHash(version.content_hash, 18)}</strong></div><div><span>Endpoint count</span><strong>{version.node_count}</strong></div><div><span>Publication</span><StatusBadge status={version.published ? "published" : "ready"} /></div></div>
      {version.parse_ok ? <div className="subscription-site-selection"><div className="subscription-selection-heading"><div><span className="panel-kicker">DEPLOY TARGETS</span><strong>Sites</strong></div><span>{selectedSiteIds.length} selected</span></div><div className="subscription-site-checks">{siteItems.map((site) => { const deployable = deployableSiteIds.includes(site.id); return <label className={!deployable ? "subscription-site-disabled" : ""} key={site.id}><input type="checkbox" checked={selectedSiteIds.includes(site.id)} disabled={!deployable} onChange={() => onToggleSite(site.id)} /><span><strong>{site.name}</strong><small>{deployable ? site.slug : "No edge node"}</small></span></label>; })}</div><Button variant="primary" disabled={!selectedSiteIds.length} onClick={onPublish}><Play size={16} /> Publish to selected sites</Button></div> : <div className="subscription-parse-error"><CircleAlert size={18} /><div><strong>Version cannot be published</strong><span>{version.parse_error || "Parsing did not produce usable outbounds."}</span></div></div>}
    </div>
  );
}

function versionLabel(version: SubscriptionVersion | undefined) {
  return version ? `v${version.version} · ${shortHash(version.content_hash, 10)}` : "-";
}
