"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleStop, TimerReset } from "lucide-react";
import { useState } from "react";
import { useMemo } from "react";
import { cancelTask, getTasks, type Task } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { label } from "../../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { ListFilters, timeRangeStart, type TimeRange } from "../../components/list-filters";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, Panel, StatusBadge } from "../../components/ui";

export default function TasksPage() {
  const { t, formatDate, formatNumber, formatPercent } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [cancelTarget, setCancelTarget] = useState<Task | null>(null);
  const [status, setStatus] = useState("");
  const [taskType, setTaskType] = useState("");
  const [timeRange, setTimeRange] = useState<TimeRange>("30d");
  const since = useMemo(() => timeRangeStart(timeRange), [timeRange]);
  const tasks = useQuery({ queryKey: ["tasks", status, taskType, since], queryFn: () => getTasks({ status: status || undefined, taskType: taskType || undefined, since }), enabled: session === true, refetchInterval: 3_000 });
  const cancel = useMutation({
    mutationFn: () => cancelTask(cancelTarget!.task_id),
    onSuccess: async () => {
      setCancelTarget(null);
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (tasks.isLoading) return <LoadingState rows={7} />;
  if (tasks.isError) return <ErrorState error={tasks.error instanceof Error ? tasks.error.message : "Unable to load tasks."} onRetry={() => void tasks.refetch()} />;

  const taskItems = tasks.data || [];
  const error = cancel.error;
  return (
    <div className="page-stack">
      <PageHeader eyebrow="DEPLOY" title="Tasks" description="Queue state, retry history, and cancellation boundaries for control-plane work." />
      {error ? <div className="inline-error" role="alert">{error instanceof Error ? error.message : "The task could not be cancelled."}</div> : null}
      <Panel>
        <div className="table-toolbar"><div className="toolbar-title"><TimerReset size={17} /><span>{t("Task queue")}</span></div><span className="toolbar-note">{t("{count} tasks", { count: formatNumber(taskItems.length) })}</span></div>
        <ListFilters timeRange={timeRange} setTimeRange={setTimeRange} selects={[{ label: "State", value: status, setValue: setStatus, options: [{ value: "", label: "All states" }, { value: "queued", label: "queued" }, { value: "running", label: "running" }, { value: "succeeded", label: "succeeded" }, { value: "failed", label: "failed" }, { value: "dead_letter", label: "dead letter" }] }, { label: "Task type", value: taskType, setValue: setTaskType, options: [{ value: "", label: "All task types" }, { value: "config.publish", label: "config publish" }, { value: "subscription.refresh", label: "subscription refresh" }, { value: "node.probe", label: "node probe" }, { value: "backup.create", label: "backup create" }] }]} />
        {taskItems.length ? <div className="table-wrap"><table><thead><tr><th>{t("Task")}</th><th>{t("State")}</th><th>{t("Progress")}</th><th>{t("Retries")}</th><th>{t("Lease")}</th><th>{t("Updated")}</th><th aria-label={t("Actions")} /></tr></thead><tbody>{taskItems.map((task) => { const canCancel = ["queued", "running"].includes(task.status); return <tr key={task.task_id}><td><strong>{t(label(task.task_type))}</strong><span className="cell-secondary mono">{task.task_id.slice(0, 14)}</span></td><td><StatusBadge status={task.status} /></td><td><div className="progress-cell"><div><span style={{ width: `${Math.max(0, Math.min(task.progress, 100))}%` }} /></div><strong>{formatPercent(task.progress)}</strong></div><span className="cell-secondary">{t(task.stage.replaceAll("_", " "))}</span></td><td>{formatNumber(task.retry_count)} / {formatNumber(task.max_retries)}</td><td>{task.locked_by || "-"}</td><td>{formatDate(task.finished_at || task.created_at)}</td><td>{canCancel ? <Button variant="ghost" size="sm" onClick={() => setCancelTarget(task)}><CircleStop size={14} /> {t("Cancel")}</Button> : null}</td></tr>; })}</tbody></table></div> : <EmptyState title="No tasks recorded" />}
      </Panel>
      <ConfirmDialog open={Boolean(cancelTarget)} onOpenChange={(open) => !open && setCancelTarget(null)} title="Cancel task" description={t("Request cancellation for {task}. Running work stops at its next safe checkpoint.", { task: cancelTarget ? t(label(cancelTarget.task_type)) : t("this task") })} confirmLabel="Cancel task" danger busy={cancel.isPending} onConfirm={() => cancel.mutate()} />
    </div>
  );
}
