"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Filter, Pencil, ServerCog } from "lucide-react";
import { useMemo, useState } from "react";
import { getNodes, getSites, updateNodeName, type Node } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { shortHash } from "../../lib/utils";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, DetailDialog, IconButton, Panel, StatusBadge } from "../../components/ui";

export default function NodesPage() {
  const { t, formatDate, formatNumber } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState("all");
  const [selectedNode, setSelectedNode] = useState<Node | null>(null);
  const [editNode, setEditNode] = useState<Node | null>(null);
  const [editName, setEditName] = useState("");
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, refetchInterval: 10_000 });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  const rename = useMutation({
    mutationFn: () => updateNodeName(editNode!.id, editName.trim()),
    onSuccess: async (updated) => {
      setEditNode(null);
      setSelectedNode((current) => current?.id === updated.id ? updated : current);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["nodes"] }),
        queryClient.invalidateQueries({ queryKey: ["proxy-configs"] }),
        queryClient.invalidateQueries({ queryKey: ["overview"] }),
      ]);
    },
  });

  const siteNames = useMemo(
    () => new Map((sites.data || []).map((site) => [site.id, site.name])),
    [sites.data],
  );
  const rows = (nodes.data || []).filter((node) => {
    if (statusFilter === "all") return true;
    if (statusFilter === "attention") return node.liveness_status !== "online" || node.config_status !== "in_sync" || node.service_status !== "healthy";
    return node.liveness_status === statusFilter;
  });

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (nodes.isLoading || sites.isLoading) return <LoadingState rows={7} />;
  if (nodes.isError || sites.isError) {
    const issue = nodes.error || sites.error;
    return <ErrorState error={issue instanceof Error ? issue.message : "The control plane did not respond."} onRetry={() => void Promise.all([nodes.refetch(), sites.refetch()])} />;
  }

  return (
    <div className="page-stack">
      <PageHeader eyebrow="OPERATE" title="Nodes" description="Heartbeat, configuration, and service facts from each monitor." />
      <Panel>
        <div className="table-toolbar">
          <div className="toolbar-title"><ServerCog size={18} /><span>{t("{count} nodes", { count: formatNumber(rows.length) })}</span></div>
          <label className="select-control"><Filter size={15} /><span className="sr-only">{t("Filter node status")}</span><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">{t("All states")}</option><option value="online">{t("Online")}</option><option value="offline">{t("Offline")}</option><option value="attention">{t("Needs attention")}</option></select></label>
        </div>
        <div className="table-wrap"><table><thead><tr><th>{t("Node")}</th><th>{t("Site")}</th><th>{t("Heartbeat")}</th><th>{t("Config")}</th><th>{t("Service")}</th><th>{t("Version")}</th><th aria-label={t("Actions")} /></tr></thead><tbody>{rows.map((node) => <tr key={node.id}><td><strong>{node.name}</strong><span className="cell-secondary mono">{node.agent_id}</span></td><td>{t(siteNames.get(node.site_id) || node.site_id)}</td><td><StatusBadge status={node.liveness_status} /></td><td><StatusBadge status={node.config_status} /></td><td><StatusBadge status={node.service_status} /></td><td><span className="version-pair">{formatNumber(node.applied_version)} <span>/</span> {formatNumber(node.desired_version)}</span></td><td><div className="row-actions"><Button size="sm" variant="ghost" onClick={() => setSelectedNode(node)}>{t("Inspect")}</Button><IconButton label={t("Rename node {name}", { name: node.name })} onClick={() => { setEditNode(node); setEditName(node.name); rename.reset(); }}><Pencil size={15} /></IconButton></div></td></tr>)}</tbody></table></div>
      </Panel>
      <DetailDialog open={Boolean(selectedNode)} onOpenChange={(open) => !open && setSelectedNode(null)} title={selectedNode?.name || "Node"} description={selectedNode ? `${t(siteNames.get(selectedNode.site_id) || selectedNode.site_id)} · ${selectedNode.agent_id}` : undefined}>
        {selectedNode ? <div className="detail-stack"><div className="status-grid"><div><span>{t("Liveness")}</span><StatusBadge status={selectedNode.liveness_status} /></div><div><span>{t("Configuration")}</span><StatusBadge status={selectedNode.config_status} /></div><div><span>{t("Service")}</span><StatusBadge status={selectedNode.service_status} /></div><div><span>{t("Subscription")}</span><StatusBadge status={selectedNode.subscription_status} /></div></div><dl className="detail-list"><div><dt>{t("Applied / desired")}</dt><dd>{formatNumber(selectedNode.applied_version)} / {formatNumber(selectedNode.desired_version)}</dd></div><div><dt>{t("Applied hash")}</dt><dd className="mono">{shortHash(selectedNode.applied_hash, 18)}</dd></div><div><dt>{t("Last heartbeat")}</dt><dd>{formatDate(selectedNode.last_seen_at)}</dd></div><div><dt>{t("Monitor")}</dt><dd>{selectedNode.monitor_version}</dd></div><div><dt>sing-box</dt><dd>{selectedNode.singbox_version}</dd></div><div><dt>{t("Advertise IP")}</dt><dd>{selectedNode.advertise_ip || "-"}</dd></div></dl>{selectedNode.last_error ? <div className="detail-error"><strong>{t("Last error")}</strong><code>{selectedNode.last_error}</code></div> : null}</div> : null}
      </DetailDialog>
      <DetailDialog open={Boolean(editNode)} onOpenChange={(open) => { if (!open && !rename.isPending) setEditNode(null); }} title="Rename node" description={editNode ? t("Change the display name for {name}. Node identity and site binding remain unchanged.", { name: editNode.name }) : undefined} contentClassName="node-edit-dialog-content">
        <form className="node-edit-form" onSubmit={(event) => { event.preventDefault(); if (editName.trim() && !rename.isPending) rename.mutate(); }}>
          <label><span>{t("Display name")}</span><input autoFocus value={editName} maxLength={128} onChange={(event) => setEditName(event.target.value)} /></label>
          {rename.error ? <div className="inline-error" role="alert">{rename.error instanceof Error ? t(rename.error.message) : t("The node name could not be updated.")}</div> : null}
          <div className="form-actions"><Button type="button" onClick={() => setEditNode(null)} disabled={rename.isPending}>{t("Cancel")}</Button><Button variant="primary" type="submit" disabled={!editName.trim() || rename.isPending}><Pencil size={15} />{rename.isPending ? t("Saving...") : t("Save changes")}</Button></div>
        </form>
      </DetailDialog>
    </div>
  );
}
