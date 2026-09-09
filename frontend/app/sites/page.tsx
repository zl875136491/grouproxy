"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Network, Pencil, Power } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { getNodes, getSites, setSiteShutdown, updateSiteName, type Site } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, DetailDialog, IconButton, Panel, StatusBadge } from "../../components/ui";

export default function SitesPage() {
  const { t, formatNumber } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [target, setTarget] = useState<Site | null>(null);
  const [editSite, setEditSite] = useState<Site | null>(null);
  const [editName, setEditName] = useState("");
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, refetchInterval: 10_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, refetchInterval: 10_000 });
  const shutdown = useMutation({
    mutationFn: ({ site, enabled }: { site: Site; enabled: boolean }) => setSiteShutdown(site.id, enabled),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["sites"] });
      setTarget(null);
    },
  });
  const rename = useMutation({
    mutationFn: () => updateSiteName(editSite!.id, editName.trim()),
    onSuccess: async () => {
      setEditSite(null);
      await queryClient.invalidateQueries({ queryKey: ["sites"] });
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
    <div className="page-stack page-fill list-page">
      <PageHeader eyebrow="POLICY" title="Sites & CIDRs" description="Regional source access is defined once and applied to local edge nodes." />
      <Panel className="list-panel">
        <div className="table-toolbar"><div className="toolbar-title"><Network size={18} /><span>{t("{count} sites", { count: formatNumber(siteItems.length) })}</span></div><span className="toolbar-note">{t("Policy changes require a release before nodes apply them.")}</span></div>
        <div className="table-wrap table-scroll"><table><thead><tr><th>{t("Site")}</th><th>{t("Edge nodes")}</th><th>{t("Policy revision")}</th><th>{t("Listener")}</th><th>{t("State")}</th><th aria-label={t("Actions")} /></tr></thead><tbody>{siteItems.map((site) => <tr key={site.id}><td><div className="cell-with-action"><strong>{t(site.name)}</strong><IconButton label={t("Rename site {name}", { name: site.name })} tooltip={false} onClick={() => { setEditSite(site); setEditName(site.name); rename.reset(); }}><Pencil size={14} /></IconButton></div><span className="cell-secondary">{site.slug}</span></td><td>{formatNumber(nodeCount.get(site.id) || 0)}</td><td>v{formatNumber(site.config_revision)}</td><td>HTTP :1080</td><td><StatusBadge status={site.shutdown ? "shutdown" : "active"} /></td><td><div className="row-actions"><Link className="button button-ghost button-sm" href={`/sites/${site.slug}/cidrs`}>{t("Open policy")} <ArrowRight size={14} /></Link><button className="row-icon-button" aria-label={t("{action} {name}", { action: t(site.shutdown ? "Restore" : "Shutdown"), name: t(site.name) })} title={t(site.shutdown ? "Restore" : "Shutdown")} onClick={() => setTarget(site)}><Power size={16} /></button></div></td></tr>)}</tbody></table></div>
      </Panel>
      <ConfirmDialog open={Boolean(target)} onOpenChange={(open) => !open && setTarget(null)} title={target?.shutdown ? "Restore site listener" : "Emergency shutdown"} description={target?.shutdown ? t("Restore proxy listener access for {name}. A release is still required for monitors to apply the state.", { name: t(target.name) }) : t("Block the proxy listener for {name}. This changes desired policy and requires a release to reach its edge nodes.", { name: t(target?.name || "") })} confirmLabel={target?.shutdown ? "Restore site" : "Shut down site"} danger={!target?.shutdown} busy={shutdown.isPending} onConfirm={() => target && shutdown.mutate({ site: target, enabled: !target.shutdown })} />
      <DetailDialog open={Boolean(editSite)} onOpenChange={(open) => { if (!open && !rename.isPending) setEditSite(null); }} title="Rename site" description={editSite ? t("Change the display name for {name}. The site slug, policy, and node bindings remain unchanged.", { name: editSite.name }) : undefined} contentClassName="node-edit-dialog-content">
        <form className="node-edit-form" onSubmit={(event) => { event.preventDefault(); if (editName.trim() && !rename.isPending) rename.mutate(); }}>
          <label><span>{t("Site display name")}</span><input autoFocus value={editName} maxLength={128} onChange={(event) => setEditName(event.target.value)} /></label>
          <div className="form-actions"><Button type="button" onClick={() => setEditSite(null)} disabled={rename.isPending}>{t("Cancel")}</Button><Button variant="primary" type="submit" disabled={!editName.trim() || rename.isPending}><Pencil size={15} />{rename.isPending ? t("Saving...") : t("Save changes")}</Button></div>
        </form>
      </DetailDialog>
    </div>
  );
}
