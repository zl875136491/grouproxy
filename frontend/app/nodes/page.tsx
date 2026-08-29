"use client";

import { useQuery } from "@tanstack/react-query";
import { Filter, ServerCog } from "lucide-react";
import { useMemo, useState } from "react";
import { getNodes, getSites, type Node } from "../../lib/api";
import { formatDate, shortHash } from "../../lib/utils";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, DetailDialog, Panel, StatusBadge } from "../../components/ui";

export default function NodesPage() {
  const session = useManagementSession();
  const [statusFilter, setStatusFilter] = useState("all");
  const [selectedNode, setSelectedNode] = useState<Node | null>(null);
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, refetchInterval: 10_000 });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });

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
          <div className="toolbar-title"><ServerCog size={18} /><span>{rows.length} nodes</span></div>
          <label className="select-control"><Filter size={15} /><span className="sr-only">Filter node status</span><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">All states</option><option value="online">Online</option><option value="offline">Offline</option><option value="attention">Needs attention</option></select></label>
        </div>
        <div className="table-wrap"><table><thead><tr><th>Node</th><th>Site</th><th>Heartbeat</th><th>Config</th><th>Service</th><th>Version</th><th aria-label="Actions" /></tr></thead><tbody>{rows.map((node) => <tr key={node.id}><td><strong>{node.name}</strong><span className="cell-secondary mono">{node.agent_id}</span></td><td>{siteNames.get(node.site_id) || node.site_id}</td><td><StatusBadge status={node.liveness_status} /></td><td><StatusBadge status={node.config_status} /></td><td><StatusBadge status={node.service_status} /></td><td><span className="version-pair">{node.applied_version} <span>/</span> {node.desired_version}</span></td><td><Button size="sm" variant="ghost" onClick={() => setSelectedNode(node)}>Inspect</Button></td></tr>)}</tbody></table></div>
      </Panel>
      <DetailDialog open={Boolean(selectedNode)} onOpenChange={(open) => !open && setSelectedNode(null)} title={selectedNode?.name || "Node"} description={selectedNode ? `${siteNames.get(selectedNode.site_id) || selectedNode.site_id} · ${selectedNode.agent_id}` : undefined}>
        {selectedNode ? <div className="detail-stack"><div className="status-grid"><div><span>Liveness</span><StatusBadge status={selectedNode.liveness_status} /></div><div><span>Configuration</span><StatusBadge status={selectedNode.config_status} /></div><div><span>Service</span><StatusBadge status={selectedNode.service_status} /></div><div><span>Subscription</span><StatusBadge status={selectedNode.subscription_status} /></div></div><dl className="detail-list"><div><dt>Applied / desired</dt><dd>{selectedNode.applied_version} / {selectedNode.desired_version}</dd></div><div><dt>Applied hash</dt><dd className="mono">{shortHash(selectedNode.applied_hash, 18)}</dd></div><div><dt>Last heartbeat</dt><dd>{formatDate(selectedNode.last_seen_at)}</dd></div><div><dt>Monitor</dt><dd>{selectedNode.monitor_version}</dd></div><div><dt>sing-box</dt><dd>{selectedNode.singbox_version}</dd></div><div><dt>Advertise IP</dt><dd>{selectedNode.advertise_ip || "-"}</dd></div></dl>{selectedNode.last_error ? <div className="detail-error"><strong>Last error</strong><code>{selectedNode.last_error}</code></div> : null}</div> : null}
      </DetailDialog>
    </div>
  );
}
