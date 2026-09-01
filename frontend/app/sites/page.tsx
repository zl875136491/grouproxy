"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Network, Power } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { getNodes, getSites, setSiteProxyAuth, setSiteShutdown, type Site } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { ConfirmDialog, Panel, StatusBadge } from "../../components/ui";

export default function SitesPage() {
  const { t, formatNumber } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [target, setTarget] = useState<Site | null>(null);
  const [authTarget, setAuthTarget] = useState<{ site: Site; required: boolean } | null>(null);
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, refetchInterval: 10_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, refetchInterval: 10_000 });
  const shutdown = useMutation({
    mutationFn: ({ site, enabled }: { site: Site; enabled: boolean }) => setSiteShutdown(site.id, enabled),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["sites"] });
      setTarget(null);
    },
  });
  const proxyAuth = useMutation({
    mutationFn: ({ site, required }: { site: Site; required: boolean }) => setSiteProxyAuth(site.id, required),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["sites"] });
      setAuthTarget(null);
    },
  });
  const nodeCount = useMemo(() => new Map((nodes.data || []).map((node) => [node.site_id, 0])), [nodes.data]);
  for (const node of nodes.data || []) nodeCount.set(node.site_id, (nodeCount.get(node.site_id) || 0) + 1);

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (sites.isLoading || nodes.isLoading) return <LoadingState rows={7} />;
  if (sites.isError || nodes.isError) {
    const issue = sites.error || nodes.error;
    return <ErrorState error={issue instanceof Error ? issue.message : "The control plane did not respond."} onRetry={() => void Promise.all([sites.refetch(), nodes.refetch()])} />;
  }

  const siteItems = sites.data || [];

  return (
    <div className="page-stack">
      <PageHeader eyebrow="POLICY" title="Sites & CIDRs" description="Regional source access is defined once and applied to local edge nodes." />
      {proxyAuth.isError ? <div className="inline-error site-auth-error" role="alert">{t(proxyAuth.error instanceof Error ? proxyAuth.error.message : "request_failed")}</div> : null}
      <Panel>
        <div className="table-toolbar"><div className="toolbar-title"><Network size={18} /><span>{t("{count} sites", { count: formatNumber(siteItems.length) })}</span></div><span className="toolbar-note">{t("Policy changes require a release before nodes apply them.")}</span></div>
        <div className="table-wrap"><table><thead><tr><th>{t("Site")}</th><th>{t("Edge nodes")}</th><th>{t("Policy revision")}</th><th>{t("Listener")}</th><th>{t("Proxy authentication")}</th><th>{t("State")}</th><th aria-label={t("Actions")} /></tr></thead><tbody>{siteItems.map((site) => <tr key={site.id}><td><strong>{t(site.name)}</strong><span className="cell-secondary">{site.slug}</span></td><td>{formatNumber(nodeCount.get(site.id) || 0)}</td><td>v{formatNumber(site.config_revision)}</td><td>HTTP :{formatNumber(site.http_port, { useGrouping: false })}</td><td><label className="matrix-toggle auth-toggle"><input type="checkbox" checked={site.proxy_auth_required} disabled={proxyAuth.isPending} onChange={(event) => setAuthTarget({ site, required: event.target.checked })} /><span aria-hidden="true" /><span className="sr-only">{t(site.proxy_auth_required ? "Disable proxy authentication for {name}" : "Enable proxy authentication for {name}", { name: t(site.name) })}</span></label></td><td><StatusBadge status={site.shutdown ? "shutdown" : "active"} /></td><td><div className="row-actions"><Link className="button button-ghost button-sm" href={`/sites/${site.slug}/cidrs`}>{t("Open policy")} <ArrowRight size={14} /></Link><button className="row-icon-button" aria-label={t("{action} {name}", { action: t(site.shutdown ? "Restore" : "Shutdown"), name: t(site.name) })} title={t(site.shutdown ? "Restore" : "Shutdown")} onClick={() => setTarget(site)}><Power size={16} /></button></div></td></tr>)}</tbody></table></div>
      </Panel>
      <ConfirmDialog open={Boolean(target)} onOpenChange={(open) => !open && setTarget(null)} title={target?.shutdown ? "Restore site listener" : "Emergency shutdown"} description={target?.shutdown ? t("Restore proxy listener access for {name}. A release is still required for monitors to apply the state.", { name: t(target.name) }) : t("Block the proxy listener for {name}. This changes desired policy and requires a release to reach its edge nodes.", { name: t(target?.name || "") })} confirmLabel={target?.shutdown ? "Restore site" : "Shut down site"} danger={!target?.shutdown} busy={shutdown.isPending} onConfirm={() => target && shutdown.mutate({ site: target, enabled: !target.shutdown })} />
      <ConfirmDialog open={Boolean(authTarget)} onOpenChange={(open) => !open && setAuthTarget(null)} title={authTarget?.required ? "Enable proxy authentication" : "Disable proxy authentication"} description={authTarget ? t(authTarget.required ? "Require HTTP Basic credentials in addition to the network allowlist for {name}." : "Allow the network allowlist without HTTP Basic credentials for {name}.", { name: t(authTarget.site.name) }) : ""} confirmLabel={authTarget?.required ? "Enable authentication" : "Disable authentication"} busy={proxyAuth.isPending} onConfirm={() => authTarget && proxyAuth.mutate(authTarget)} />
    </div>
  );
}
