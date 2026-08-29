"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleStop, TimerReset } from "lucide-react";
import { useState } from "react";
import { cancelTask, getTasks, type Task } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { formatDate, label } from "../../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, Panel, StatusBadge } from "../../components/ui";

export default function TasksPage() {
  const { t } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [cancelTarget, setCancelTarget] = useState<Task | null>(null);
  const tasks = useQuery({ queryKey: ["tasks"], queryFn: getTasks, enabled: session === true, refetchInterval: 3_000 });
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
        {taskItems.length ? <div className="table-wrap"><table><thead><tr><th>{t("Task")}</th><th>{t("State")}</th><th>{t("Progress")}</th><th>{t("Retries")}</th><th>{t("Lease")}</th><th>{t("Updated")}</th><th aria-label={t("Actions")} /></tr></thead><tbody>{taskItems.map((task) => { const canCancel = ["queued", "running"].includes(task.status); return <tr key={task.task_id}><td><strong>{t(label(task.task_type))}</strong><span className="cell-secondary mono">{task.task_id.slice(0, 14)}</span></td><td><StatusBadge status={task.status} /></td><td><div className="progress-cell"><div><span style={{ width: `${Math.max(0, Math.min(task.progress, 100))}%` }} /></div><strong>{task.progress}%</strong></div><span className="cell-secondary">{t(task.stage.replaceAll("_", " "))}</span></td><td>{task.retry_count} / {task.max_retries}</td><td>{task.locked_by || "-"}</td><td>{formatDate(task.finished_at || task.created_at)}</td><td>{canCancel ? <Button variant="ghost" size="sm" onClick={() => setCancelTarget(task)}><CircleStop size={14} /> {t("Cancel")}</Button> : null}</td></tr>; })}</tbody></table></div> : <EmptyState title="No tasks recorded" />}
      </Panel>
      <ConfirmDialog open={Boolean(cancelTarget)} onOpenChange={(open) => !open && setCancelTarget(null)} title="Cancel task" description={t("Request cancellation for {task}. Running work stops at its next safe checkpoint.", { task: cancelTarget ? t(label(cancelTarget.task_type)) : t("this task") })} confirmLabel="Cancel task" danger busy={cancel.isPending} onConfirm={() => cancel.mutate()} />
    </div>
  );
}
