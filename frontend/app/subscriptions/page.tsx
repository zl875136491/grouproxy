"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CirclePlus, Copy, Download, Filter, Play, WrapText } from "lucide-react";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  createSubscriptionSource,
  createSingleNodeSubscription,
  getSubscriptionVersionContent,
  getNodes,
  getSites,
  getSubscriptions,
  getTask,
  publishSubscriptionVersion,
  refreshSubscription,
  uploadSubscription,
  type SubscriptionSource,
  type SubscriptionVersion,
  type Task,
} from "../../lib/api";
import { writeClipboard } from "../../lib/clipboard";
import { usePreferences } from "../../lib/preferences";
import { shortHash } from "../../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { FilterSelect } from "../../components/list-filters";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { toastErrorMessage, useToast } from "../../components/toast";
import { Button, ConfirmDialog, DetailDialog, Panel, RefreshButton, StatusBadge } from "../../components/ui";

type FormMode = "add" | null;
type SourceType = "http" | "single_node" | "upload";
type SourceScheme = "http" | "https";

function composeSourceURL(scheme: SourceScheme, value: string) {
  const trimmed = value.trim();
  const withoutScheme = trimmed.replace(/^(https?):\/\//i, "");
  return `${scheme}://${withoutScheme}`;
}

function schemeFromURL(value: string): SourceScheme | null {
  const match = value.trim().match(/^(https?):\/\//i);
  return match ? (match[1].toLowerCase() as SourceScheme) : null;
}

function applySourceURLInput(value: string): { scheme?: SourceScheme; url: string } {
  const detected = schemeFromURL(value);
  if (!detected) return { url: value };
  return { scheme: detected, url: value.trim().replace(/^(https?):\/\//i, "") };
}

type SourceStateFilter = "" | "current" | "failed" | "pending";

function sourceRefreshState(source: SubscriptionSource): Exclude<SourceStateFilter, ""> {
  if (source.last_refresh_error) return "failed";
  return source.last_refresh_at ? "current" : "pending";
}

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
  const [sourceType, setSourceType] = useState<SourceType>("http");
  const [sourceTypeFilter, setSourceTypeFilter] = useState<"" | SourceType>("");
  const [sourceStateFilter, setSourceStateFilter] = useState<SourceStateFilter>("");
  const [sourceName, setSourceName] = useState("");
  const [sourceScheme, setSourceScheme] = useState<SourceScheme>("http");
  const [sourceURL, setSourceURL] = useState("");
  const [fetchInterval, setFetchInterval] = useState("21600");
  const [singleNodeURI, setSingleNodeURI] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [selectedSourceId, setSelectedSourceId] = useState("");
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [selectedSiteIds, setSelectedSiteIds] = useState<string[]>([]);
  const [publishTarget, setPublishTarget] = useState<SubscriptionVersion | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<SubscriptionVersion | null>(null);

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
      url: composeSourceURL(sourceScheme, sourceURL),
      scheme: sourceScheme,
      fetch_interval_sec: Number(fetchInterval),
    }),
    onSuccess: async (result) => {
      setSourceName("");
      setSourceScheme("http");
      setSourceURL("");
      setSingleNodeURI("");
      setFormMode(null);
      setSelectedSourceId(result.source.id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["subscriptions"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      ]);
    },
  });
  const singleNodeCreate = useMutation({
    mutationFn: () => createSingleNodeSubscription(sourceName.trim(), singleNodeURI.trim()),
    onSuccess: async (result) => {
      setSourceName("");
      setSingleNodeURI("");
      setFormMode(null);
      setSelectedSourceId(result.source.id);
      setSelectedVersionId(result.version.id);
      await queryClient.invalidateQueries({ queryKey: ["subscriptions"] });
    },
  });
  const upload = useMutation({
    mutationFn: () => uploadSubscription(sourceName.trim(), uploadFile!),
    onSuccess: async (result) => {
      setSourceName("");
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
      setConfirmTarget(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["subscriptions"] }),
        queryClient.invalidateQueries({ queryKey: ["releases"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
        queryClient.invalidateQueries({ queryKey: ["nodes"] }),
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
  const filteredSourceItems = useMemo(
    () => sourceItems.filter((source) => (
      (!sourceTypeFilter || source.source_type === sourceTypeFilter)
      && (!sourceStateFilter || sourceRefreshState(source) === sourceStateFilter)
    )).slice().sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at)),
    [sourceItems, sourceStateFilter, sourceTypeFilter],
  );

  useEffect(() => {
    if (!selectedSourceId && filteredSourceItems[0]) setSelectedSourceId(filteredSourceItems[0].id);
  }, [filteredSourceItems, selectedSourceId]);

  useEffect(() => {
    if (!currentVersion) return;
    if (!selectedVersionId || !currentVersions.some((item) => item.id === selectedVersionId)) {
      setSelectedVersionId(currentVersion.id);
    }
  }, [currentVersion, currentVersions, selectedVersionId]);

  const previousVersionId = useRef("");
  useEffect(() => {
    // Site targets are an explicit operator choice. Never carry a selection
    // from another immutable version into the next publish operation.
    if (!currentVersion) return;
    if (previousVersionId.current !== currentVersion.id) {
      previousVersionId.current = currentVersion.id;
      setSelectedSiteIds([]);
    }
  }, [currentVersion]);

  function submitSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sourceType === "http" && sourceName.trim() && sourceURL.trim()) sourceCreate.mutate();
    if (sourceType === "single_node" && singleNodeURI.trim()) singleNodeCreate.mutate();
    if (sourceType === "upload" && sourceName.trim() && uploadFile) upload.mutate();
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

  const sourcePending = sourceCreate.isPending || singleNodeCreate.isPending || upload.isPending;

  return (
    <div className="page-stack page-fill subscriptions-page">
      <PageHeader
        eyebrow="DEPLOY"
        title="Subscriptions"
        description="Versioned upstream content is selected per site and released through the normal node ACK path."
        actions={<>
          <Button
            disabled={!currentVersion || !currentVersion.parse_ok}
            onClick={() => {
              if (!currentVersion) return;
              setSelectedSiteIds([]);
              setPublishTarget(currentVersion);
            }}
          >
            <Play size={16} /> {t("Publish")}
          </Button>
          <Button variant="primary" onClick={() => { sourceCreate.reset(); singleNodeCreate.reset(); upload.reset(); setSourceType("http"); setSourceScheme("http"); setFormMode("add"); }}><CirclePlus size={16} /> {t("Add source")}</Button>
        </>}
      />
      <DetailDialog open={formMode === "add"} onOpenChange={(open) => { if (!open && !sourcePending) setFormMode(null); }} title="Add source" description="Choose a subscription site connection or paste one VLESS / VMess node." contentClassName="source-dialog-content">
        <form className="source-dialog-form" onSubmit={submitSource}>
          <fieldset className="source-type-radios">
            <legend>{t("Source type")}</legend>
            <label><input type="radio" name="source-type" checked={sourceType === "http"} onChange={() => setSourceType("http")} /><span><strong>{t("Subscription site connection")}</strong><small>HTTP / HTTPS</small></span></label>
            <label><input type="radio" name="source-type" checked={sourceType === "single_node"} onChange={() => setSourceType("single_node")} /><span><strong>{t("Single node")}</strong><small>VLESS / VMess</small></span></label>
            <label><input type="radio" name="source-type" checked={sourceType === "upload"} onChange={() => setSourceType("upload")} /><span><strong>{t("Subscription file")}</strong><small>YAML / JSON</small></span></label>
          </fieldset>
          <label><span>{t(sourceType === "single_node" ? "Name (optional)" : "Name")}</span><input autoFocus value={sourceName} maxLength={120} onChange={(event) => setSourceName(event.target.value)} placeholder={sourceType === "http" ? t("Regional upstream") : sourceType === "upload" ? t("Imported upstream") : t("VLESS Reality Vision")} /></label>
          {sourceType === "http" ? <><div className="source-url-row"><label><span>{t("Protocol")}</span><select value={sourceScheme} onChange={(event) => { setSourceScheme(event.target.value as SourceScheme); setSourceURL((current) => current.replace(/^(https?):\/\//i, "")); }}><option value="http">HTTP</option><option value="https">HTTPS</option></select></label><label className="source-url-field"><span>{t("Subscription URL")}</span><input value={sourceURL} onChange={(event) => { const next = applySourceURLInput(event.target.value); if (next.scheme) setSourceScheme(next.scheme); setSourceURL(next.url); }} placeholder="upstream.example/subscription" autoComplete="off" /></label></div><label><span>{t("Refresh interval")}</span><select value={fetchInterval} onChange={(event) => setFetchInterval(event.target.value)}><option value="3600">{t("1 hour")}</option><option value="21600">{t("6 hours")}</option><option value="86400">{t("24 hours")}</option></select></label></> : sourceType === "single_node" ? <label><span>{t("Single-node URI")}</span><textarea value={singleNodeURI} onChange={(event) => setSingleNodeURI(event.target.value)} placeholder="vless://uuid@host:port?... or vmess://base64-json" spellCheck={false} /></label> : <label><span>{t("Subscription file")}</span><input type="file" accept=".json,.yaml,.yml,application/json,application/x-yaml,text/yaml" onChange={(event) => setUploadFile(event.target.files?.[0] || null)} /></label>}
          <div className="form-actions"><Button type="button" disabled={sourcePending} onClick={() => setFormMode(null)}>{t("Cancel")}</Button><Button variant="primary" type="submit" disabled={sourcePending || (sourceType === "http" ? !sourceName.trim() || !sourceURL.trim() : sourceType === "single_node" ? !singleNodeURI.trim() : !sourceName.trim() || !uploadFile)}>{sourcePending ? t("Adding...") : t("Add source")}</Button></div>
        </form>
      </DetailDialog>
      <div className="subscription-scroll-area">
      <section className="subscription-layout">
        <div className="subscription-source-column">
          <Panel className="subscription-filter-panel">
            <div className="subscription-filter-heading">
              <div className="toolbar-title"><Filter size={16} /><span>{t("Filter sources")}</span></div>
              <span className="toolbar-note">{t("{count} shown", { count: formatNumber(filteredSourceItems.length) })}</span>
            </div>
            <div className="subscription-filters">
              <FilterSelect label="Source type" value={sourceTypeFilter} setValue={(value) => setSourceTypeFilter(value as "" | SourceType)} options={[{ value: "", label: "All types" }, { value: "http", label: "Subscription site connection" }, { value: "single_node", label: "Single node" }, { value: "upload", label: "Subscription file" }]} />
              <FilterSelect label="State" value={sourceStateFilter} setValue={(value) => setSourceStateFilter(value as SourceStateFilter)} options={[{ value: "", label: "All states" }, { value: "current", label: "current" }, { value: "failed", label: "failed" }, { value: "pending", label: "pending" }]} />
            </div>
          </Panel>
          <Panel className="list-panel subscription-source-panel">
            <div className="subscription-source-heading"><div><span className="panel-kicker">{t("UPSTREAMS")}</span><h2>{t("Sources")}</h2></div></div>
            {sourceItems.length ? filteredSourceItems.length ? <div className="subscription-source-list">{filteredSourceItems.map((source) => {
              const state = sourceRefreshState(source);
              return <div className={`subscription-source-row ${currentSource?.id === source.id ? "subscription-source-selected" : ""}`} key={source.id}>
                <button type="button" className="subscription-source-select" onClick={() => { setSelectedSourceId(source.id); setSelectedVersionId(""); }}>
                  <span>
                    <strong>{source.name}</strong>
                    <small>{source.source_type === "http" ? source.url_hint : t(source.source_type === "single_node" ? "Single VLESS or VMess node" : "Imported file")}</small>
                    <small>{t("Created {date}", { date: formatDate(source.created_at) })} · {t("Updated {date}", { date: formatDate(source.updated_at) })}</small>
                  </span>
                </button>
                <div className="subscription-source-actions">
                  {source.refreshable ? <RefreshButton label={t("Refresh {name}", { name: source.name })} disabled={refresh.isPending} onRefresh={async () => { const result = await refresh.mutateAsync(source.id); return waitForRefreshTask(result.task.task_id); }} /> : null}
                  <StatusBadge status={state} />
                </div>
              </div>;
            })}</div> : <EmptyState title="No matching sources" detail="Change the source type or state filter." /> : <EmptyState title="No subscription sources" detail="Add a subscription site or a single node." />}
          </Panel>
        </div>
        <Panel className="subscription-detail-panel">
          {currentVersion ? <VersionDetail version={currentVersion} sourceName={currentSource?.name || ""} /> : <EmptyState title="Select a source version" detail="Parsed versions are available after a refresh or file import." />}
        </Panel>
      </section>
      </div>
      <PublishSitesDialog version={publishTarget} sourceName={currentSource?.name || ""} sites={siteItems} deployableSiteIds={deployableSiteIds} selectedSiteIds={selectedSiteIds} onToggleSite={toggleSite} open={Boolean(publishTarget)} onOpenChange={(open) => { if (!open) setPublishTarget(null); }} onContinue={() => { if (publishTarget) { setConfirmTarget(publishTarget); setPublishTarget(null); } }} />
      <ConfirmDialog open={Boolean(confirmTarget)} onOpenChange={(open) => { if (!open) setConfirmTarget(null); }} title="Publish subscription version" description={confirmTarget ? t("Release {version} to {count} selected sites. Each edge node validates and acknowledges the new outbound configuration independently.", { version: versionLabel(confirmTarget, formatNumber), count: formatNumber(selectedSiteIds.length) }) : ""} confirmLabel="Publish version" busy={publish.isPending} onConfirm={() => confirmTarget && publish.mutate(confirmTarget)} />
    </div>
  );
}

function VersionDetail({
  version,
  sourceName,
}: {
  version: SubscriptionVersion;
  sourceName: string;
}) {
  const { t, formatBytes, formatDate, formatNumber } = usePreferences();

  return (
    <div className="subscription-detail subscription-detail-split">
      <section className="subscription-detail-summary">
        <div className="release-detail-heading"><div><span className="panel-kicker">{t("SELECTED VERSION")}</span><h2>{sourceName}</h2><p>{version.format} · {formatBytes(version.size_bytes)} · {t("Fetched {date}", { date: formatDate(version.fetched_at) })}</p></div><StatusBadge status={version.parse_ok ? "valid" : "failed"} /></div>
        <div className="detail-summary-grid"><div><span>{t("Version")}</span><strong>{versionLabel(version, formatNumber)}</strong></div><div><span>{t("Content hash")}</span><strong className="mono" title={version.content_hash}>{shortHash(version.content_hash, 18)}</strong></div><div><span>{t("Endpoint count")}</span><strong>{formatNumber(version.node_count)}</strong></div><div><span>{t("Publication")}</span><StatusBadge status={version.published ? "published" : "ready"} /></div></div>
      </section>
      <InlineVersionDocument version={version} />
    </div>
  );
}

function InlineVersionDocument({ version }: { version: SubscriptionVersion }) {
  const { t } = usePreferences();
  const { toast } = useToast();
  const [wrapLines, setWrapLines] = useState(false);
  const content = useQuery({
    queryKey: ["subscription-version-content", version.source_id, version.id],
    queryFn: () => getSubscriptionVersionContent(version.source_id, version.id),
    staleTime: 5 * 60_000,
  });

  async function copyContent() {
    if (!content.data?.content) return;
    try {
      await writeClipboard(content.data.content);
      toast({ title: t("Copied"), variant: "success" });
    } catch (error) {
      toast({ title: t("Operation failed"), description: t(toastErrorMessage(error, "Unable to copy source content.")), variant: "destructive" });
    }
  }

  function downloadContent() {
    if (!content.data?.content) return;
    const extension = version.format === "clash" ? "yaml" : version.format === "unknown" ? "txt" : "json";
    const url = URL.createObjectURL(new Blob([content.data.content], { type: "text/plain;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `subscription-v${version.version}.${extension}`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return <section className="subscription-document-section"><div className="subscription-document-heading"><div><span className="panel-kicker">{t("Source content")}</span><strong>{version.format.toUpperCase()}</strong></div><div className="row-actions"><Button size="sm" aria-pressed={wrapLines} onClick={() => setWrapLines((value) => !value)}><WrapText size={14} />{t(wrapLines ? "No wrap" : "Wrap lines")}</Button><Button size="sm" disabled={!content.data?.content} onClick={() => void copyContent()}><Copy size={14} />{t("Copy")}</Button><Button size="sm" disabled={!content.data?.content} onClick={downloadContent}><Download size={14} />{t("Download")}</Button></div></div>{content.isLoading ? <LoadingState rows={8} /> : content.isError ? <ErrorState error={content.error instanceof Error ? content.error.message : t("Unable to load source content.")} onRetry={() => void content.refetch()} /> : content.data ? <HighlightedDocument content={content.data.content} format={version.format} wrapLines={wrapLines} /> : <EmptyState title={t("No source content")} />}</section>;
}

function PublishSitesDialog({
  version,
  sourceName,
  sites,
  deployableSiteIds,
  selectedSiteIds,
  onToggleSite,
  open,
  onOpenChange,
  onContinue,
}: {
  version: SubscriptionVersion | null;
  sourceName: string;
  sites: Array<{ id: string; name: string; slug: string }>;
  deployableSiteIds: string[];
  selectedSiteIds: string[];
  onToggleSite: (siteId: string) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onContinue: () => void;
}) {
  const { t, formatDate, formatNumber } = usePreferences();
  return <DetailDialog open={open} onOpenChange={onOpenChange} title="Publish subscription version" description="Review the selected source and choose one or more deployment sites." contentClassName="publish-sites-dialog">
    {version ? <div className="publish-sites-form">
      <dl className="publish-source-facts"><div><dt>{t("Source")}</dt><dd>{sourceName}</dd></div><div><dt>{t("Fetched")}</dt><dd>{formatDate(version.fetched_at)}</dd></div><div><dt>{t("Publication")}</dt><dd><StatusBadge status={version.published ? "published" : "ready"} /></dd></div></dl>
      <div className="subscription-selection-heading"><div><span className="panel-kicker">{t("DEPLOY TARGETS")}</span><strong>{t("Sites")}</strong></div><span>{t("{count} selected", { count: formatNumber(selectedSiteIds.length) })}</span></div>
      <div className="subscription-site-checks publish-site-checks" role="group" aria-label={t("Deployment sites")}>{sites.map((site) => { const deployable = deployableSiteIds.includes(site.id); const selected = selectedSiteIds.includes(site.id); return <label className={`${!deployable ? "subscription-site-disabled" : ""} ${selected ? "subscription-site-selected" : ""}`} key={site.id}><input className="site-radio-input" type="checkbox" checked={selected} disabled={!deployable} onChange={() => onToggleSite(site.id)} /><span className="site-choice-copy"><strong>{t(site.name)}</strong><small>{deployable ? site.slug : t("No edge node")}</small></span></label>; })}</div>
      <p className="subscription-selection-hint">{t("Choose one or more sites to create a release. No sites are selected by default.")}</p>
      <div className="form-actions"><Button type="button" onClick={() => onOpenChange(false)}>{t("Cancel")}</Button><Button variant="primary" disabled={!selectedSiteIds.length} onClick={onContinue}><Play size={15} /> {t("Continue")}</Button></div>
    </div> : null}
  </DetailDialog>;
}

function HighlightedDocument({ content, format, wrapLines }: { content: string; format: string; wrapLines: boolean }) {
  const language = format === "clash" ? "yaml" : format === "unknown" ? "text" : "json";
  return <pre className={`source-document source-document-${language} ${wrapLines ? "source-document-wrap" : ""}`} data-language={language}><code>{content.split(/\r?\n/).map((line, index, lines) => <span className="source-code-line" key={`${index}-${line.slice(0, 12)}`}>{highlightLine(line, index)}{index < lines.length - 1 ? "\n" : null}</span>)}</code></pre>;
}

function highlightLine(line: string, lineIndex: number) {
  const tokenPattern = /(#[^\n]*|\/\/[^\n]*|"(?:\\.|[^"\\])*"|'(?:''|[^'])*'|\b(?:true|false|null|yes|no|on|off)\b|-?\b\d+(?:\.\d+)?\b|[A-Za-z_][A-Za-z0-9_.-]*(?=\s*:))/g;
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = tokenPattern.exec(line))) {
    if (match.index > cursor) parts.push(line.slice(cursor, match.index));
    const token = match[0];
    const className = token.startsWith("#") || token.startsWith("//")
      ? "source-token-comment"
      : token.startsWith("\"") || token.startsWith("'")
        ? "source-token-string"
        : /^(true|false|null|yes|no|on|off)$/i.test(token)
          ? "source-token-bool"
          : /^-?\d/.test(token)
            ? "source-token-number"
            : "source-token-key";
    parts.push(<span className={className} key={`${lineIndex}-${match.index}`}>{token}</span>);
    cursor = match.index + token.length;
  }
  if (cursor < line.length) parts.push(line.slice(cursor));
  return parts;
}

function versionLabel(
  version: SubscriptionVersion | undefined,
  formatNumber: (value: number | null | undefined) => string = (value) => String(value ?? "-"),
) {
  return version ? `v${formatNumber(version.version)} · ${shortHash(version.content_hash, 10)}` : "-";
}
