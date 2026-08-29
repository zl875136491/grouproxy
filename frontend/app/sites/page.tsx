"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Network, Power } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { getNodes, getSites, setSiteShutdown, type Site } from "../../lib/api";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { ConfirmDialog, Panel, StatusBadge } from "../../components/ui";

export default function SitesPage() {
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [target, setTarget] = useState<Site | null>(null);
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, refetchInterval: 10_000 });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, refetchInterval: 10_000 });
  const shutdown = useMutation({
    mutationFn: ({ site, enabled }: { site: Site; enabled: boolean }) => setSiteShutdown(site.id, enabled),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["sites"] });
      setTarget(null);
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
      <Panel>
        <div className="table-toolbar"><div className="toolbar-title"><Network size={18} /><span>{siteItems.length} sites</span></div><span className="toolbar-note">Policy changes require a release before nodes apply them.</span></div>
        <div className="table-wrap"><table><thead><tr><th>Site</th><th>Edge nodes</th><th>Policy revision</th><th>Listener</th><th>State</th><th aria-label="Actions" /></tr></thead><tbody>{siteItems.map((site) => <tr key={site.id}><td><strong>{site.name}</strong><span className="cell-secondary">{site.slug}</span></td><td>{nodeCount.get(site.id) || 0}</td><td>v{site.config_revision}</td><td>HTTP :{site.http_port}</td><td><StatusBadge status={site.shutdown ? "shutdown" : "active"} /></td><td><div className="row-actions"><Link className="button button-ghost button-sm" href={`/sites/${site.slug}/cidrs`}>Open policy <ArrowRight size={14} /></Link><button className="row-icon-button" aria-label={`${site.shutdown ? "Restore" : "Shutdown"} ${site.name}`} title={`${site.shutdown ? "Restore" : "Shutdown"} ${site.name}`} onClick={() => setTarget(site)}><Power size={16} /></button></div></td></tr>)}</tbody></table></div>
      </Panel>
      <ConfirmDialog open={Boolean(target)} onOpenChange={(open) => !open && setTarget(null)} title={target?.shutdown ? "Restore site listener" : "Emergency shutdown"} description={target?.shutdown ? `Restore proxy listener access for ${target.name}. A release is still required for monitors to apply the state.` : `Block the proxy listener for ${target?.name}. This changes desired policy and requires a release to reach its edge nodes.`} confirmLabel={target?.shutdown ? "Restore site" : "Shut down site"} danger={!target?.shutdown} busy={shutdown.isPending} onConfirm={() => target && shutdown.mutate({ site: target, enabled: !target.shutdown })} />
    </div>
  );
}
