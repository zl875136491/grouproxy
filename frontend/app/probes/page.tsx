"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleCheck, CircleX, Gauge, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createNodeProbe, getNodeProbes, getNodes, getTask, type Node, type ProbeOverview, type Task } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, Panel, RefreshButton, StatusBadge } from "../../components/ui";

function isFinishedProbeTask(task: Task | undefined) {
  return Boolean(task && ["succeeded", "failed", "cancelled", "dead_letter"].includes(task.status));
}

export default function ProbesPage() {
  const { t, formatDate, formatDuration, formatNumber } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, refetchInterval: 10_000 });
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const [targetURL, setTargetURL] = useState("https://example.com/");
  const [probeTaskId, setProbeTaskId] = useState<string | null>(null);
  const completedTaskRef = useRef<string | null>(null);
  const probeStartLock = useRef(false);
  useEffect(() => {
    if (!selectedNodeId && nodes.data?.[0]) setSelectedNodeId(nodes.data[0].id);
  }, [nodes.data, selectedNodeId]);
  useEffect(() => {
    setProbeTaskId(null);
    completedTaskRef.current = null;
  }, [selectedNodeId]);
  const probes = useQuery({ queryKey: ["probes", selectedNodeId], queryFn: () => getNodeProbes(selectedNodeId), enabled: session === true && Boolean(selectedNodeId), refetchInterval: 5_000 });
  const probeTask = useQuery({
    queryKey: ["task", probeTaskId],
    queryFn: () => getTask(probeTaskId!),
    enabled: session === true && Boolean(probeTaskId),
    refetchInterval: (query) => isFinishedProbeTask(query.state.data as Task | undefined) ? false : 1_500,
  });
  const runProbe = useMutation({
    mutationFn: () => createNodeProbe(selectedNodeId, { target_url: targetURL }),
    onSuccess: async (task) => {
      setProbeTaskId(task.task_id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["probes", selectedNodeId] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      ]);
    },
    onSettled: () => {
      probeStartLock.current = false;
    },
  });
  const trackedTask = probeTask.data || runProbe.data;
  const probeIsActive = Boolean(trackedTask && !isFinishedProbeTask(trackedTask));
  useEffect(() => {
    if (!trackedTask || !isFinishedProbeTask(trackedTask) || completedTaskRef.current === trackedTask.task_id) return;
    completedTaskRef.current = trackedTask.task_id;
    void Promise.all([
      probes.refetch(),
      queryClient.invalidateQueries({ queryKey: ["tasks"] }),
    ]);
  }, [probes, queryClient, trackedTask]);

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (nodes.isLoading) return <LoadingState rows={7} />;
  if (nodes.isError) return <ErrorState error="Unable to load nodes." onRetry={() => void nodes.refetch()} />;
  const nodeItems = nodes.data || [];
  const selectedNode = nodeItems.find((node) => node.id === selectedNodeId);
  const data = probes.data;
  const taskIcon = trackedTask?.status === "succeeded" ? <CircleCheck size={16} /> : isFinishedProbeTask(trackedTask) ? <CircleX size={16} /> : <Gauge size={16} />;
  const taskError = runProbe.error || probeTask.error;
  function startProbe() {
    if (probeStartLock.current || runProbe.isPending || probeIsActive || !selectedNodeId) return;
    probeStartLock.current = true;
    runProbe.mutate();
  }

  return <div className="page-stack"><PageHeader eyebrow="OBSERVE" title="Outbound probes" description="Per-outbound health and circuit-breaker state." actions={<RefreshButton label="Refresh" disabled={!selectedNodeId} onRefresh={() => Promise.all([probes.refetch(), nodes.refetch()])} />} /><Panel className="probe-controls"><div className="inline-form"><label><span>{t("Node")}</span><select value={selectedNodeId} onChange={(event) => setSelectedNodeId(event.target.value)}><option value="">{t("No node selected.")}</option>{nodeItems.map((node) => <option value={node.id} key={node.id}>{node.name}</option>)}</select></label><label><span>{t("Target URL")}</span><input value={targetURL} onChange={(event) => setTargetURL(event.target.value)} /></label><Button variant="primary" disabled={!selectedNodeId || runProbe.isPending || probeIsActive} onClick={startProbe}><Play size={15} />{runProbe.isPending || probeIsActive ? t("Working...") : t("Run probe")}</Button></div>{trackedTask ? <div className="change-summary" role="status">{taskIcon}<span>{t("Probe state")}</span><StatusBadge status={trackedTask.status} /></div> : null}{taskError ? <div className="inline-error" role="alert">{taskError instanceof Error ? t(taskError.message) : t("The probe task could not be created.")}</div> : null}</Panel>{selectedNode && data ? <ProbeDetails node={selectedNode} data={data} formatDate={formatDate} formatDuration={formatDuration} formatNumber={formatNumber} t={t} /> : selectedNode && probes.isError ? <ErrorState error={probes.error instanceof Error ? probes.error.message : "Unable to load probe results."} onRetry={() => void probes.refetch()} /> : <Panel><EmptyState title="No node selected." /></Panel>}</div>;
}

function ProbeDetails({ node, data, formatDate, formatDuration, formatNumber, t }: { node: Node; data: ProbeOverview; formatDate: (value: string | null | undefined, withTime?: boolean) => string; formatDuration: (value: number | null | undefined) => string; formatNumber: (value: number | null | undefined, options?: Intl.NumberFormatOptions) => string; t: (key: string, values?: Record<string, string | number>) => string }) {
  return <section className="dashboard-grid dashboard-grid-primary"><Panel><div className="panel-heading"><div><span className="panel-kicker">{t("Circuit")}</span><h2>{node.name}</h2></div><StatusBadge status={node.probe_status} /></div><div className="table-wrap"><table><thead><tr><th>{t("Destination")}</th><th>{t("Circuit")}</th><th>{t("Failure")}</th><th>{t("Success")}</th><th>{t("Latency")}</th></tr></thead><tbody>{data.circuits.length ? data.circuits.map((circuit) => <tr key={circuit.outbound_tag}><td className="mono">{circuit.outbound_tag}</td><td><StatusBadge status={circuit.state} /></td><td>{formatNumber(circuit.consecutive_failures)}</td><td>{formatNumber(circuit.consecutive_successes)}</td><td>{formatDuration(circuit.last_latency_ms)}</td></tr>) : <tr><td colSpan={5}><EmptyState title="No probe results recorded." /></td></tr>}</tbody></table></div></Panel><Panel><div className="panel-heading"><div><span className="panel-kicker">{t("History")}</span><h2>{t("Recent probes")}</h2></div><Gauge size={18} /></div><div className="activity-list">{data.history.length ? data.history.slice(0, 12).map((item) => <div className="activity-row" key={item.id}><span className="activity-icon">{item.success ? <CircleCheck size={16} /> : <CircleX size={16} />}</span><div><strong>{item.outbound_tag}</strong><span>{formatDate(item.sampled_at)} · {formatDuration(item.latency_ms)}</span></div><StatusBadge status={item.success ? "succeeded" : "failed"} /></div>) : <EmptyState title="No probe results recorded." />}</div></Panel></section>;
}
