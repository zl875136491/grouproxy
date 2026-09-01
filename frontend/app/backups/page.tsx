"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, CheckCircle2, DatabaseBackup, RotateCcw } from "lucide-react";
import { useState } from "react";
import {
  createBackup,
  getBackups,
  restoreBackup,
  type BackupRecord,
} from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { shortHash } from "../../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, Panel, StatusBadge } from "../../components/ui";

export default function BackupsPage() {
  const { t, formatBytes, formatDate, formatNumber } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [rehearsalTarget, setRehearsalTarget] = useState<BackupRecord | null>(null);
  const [restoreTarget, setRestoreTarget] = useState<BackupRecord | null>(null);
  const backups = useQuery({
    queryKey: ["backups"],
    queryFn: getBackups,
    enabled: session === true,
    refetchInterval: 3_000,
  });
  const create = useMutation({
    mutationFn: createBackup,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["backups"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      ]);
    },
  });
  const restore = useMutation({
    mutationFn: ({ backupId, confirm }: { backupId: string; confirm: boolean }) => restoreBackup(backupId, confirm),
    onSuccess: async () => {
      setRehearsalTarget(null);
      setRestoreTarget(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["backups"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      ]);
    },
  });

  if (session === null) return <LoadingState rows={8} />;
  if (!session) return <SessionGate />;
  if (backups.isLoading) return <LoadingState rows={8} />;
  if (backups.isError) {
    return <ErrorState error={backups.error instanceof Error ? backups.error.message : "Unable to load backups."} onRetry={() => void backups.refetch()} />;
  }

  const records = backups.data || [];
  const mutationError = create.error || restore.error;
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="GOVERN"
        title="Backups"
        description="Verified control-plane snapshots with a rehearsal-first restore workflow."
        actions={<Button variant="primary" onClick={() => create.mutate()} disabled={create.isPending}><DatabaseBackup size={16} /> {create.isPending ? t("Creating backup...") : t("Create backup")}</Button>}
      />
      {mutationError ? <div className="inline-error" role="alert">{mutationError instanceof Error ? t(mutationError.message) : t("The backup operation was not accepted.")}</div> : null}
      <Panel>
        <div className="table-toolbar"><div className="toolbar-title"><Archive size={18} /><span>{t("Backups: {count}", { count: formatNumber(records.length) })}</span></div><span className="toolbar-note">{t("Every archive includes a manifest and SHA-256 checksum.")}</span></div>
        {records.length ? <div className="table-wrap"><table><thead><tr><th>{t("Created")}</th><th>{t("Backup")}</th><th>{t("Origin")}</th><th>{t("Scope")}</th><th>{t("Size")}</th><th>{t("Encryption")}</th><th>{t("Last rehearsal")}</th><th>{t("State")}</th><th>{t("Checksum")}</th><th aria-label={t("Actions")} /></tr></thead><tbody>{records.map((record) => <BackupRow key={record.backup_id} record={record} formatBytes={formatBytes} formatDate={formatDate} t={t} onRehearse={() => setRehearsalTarget(record)} onRestore={() => setRestoreTarget(record)} />)}</tbody></table></div> : <EmptyState title="No backups recorded" detail="Create a snapshot before a risky change or upgrade." />}
      </Panel>
      <ConfirmDialog open={Boolean(rehearsalTarget)} onOpenChange={(open) => !open && setRehearsalTarget(null)} title="Run restore rehearsal" description={rehearsalTarget ? t("Verify {backup} without changing live data. The task checks the archive manifest and collection hashes.", { backup: shortHash(rehearsalTarget.backup_id, 18) }) : ""} confirmLabel="Run rehearsal" busy={restore.isPending} onConfirm={() => rehearsalTarget && restore.mutate({ backupId: rehearsalTarget.backup_id, confirm: false })} />
      <ConfirmDialog open={Boolean(restoreTarget)} onOpenChange={(open) => !open && setRestoreTarget(null)} title="Restore backup" description={restoreTarget ? t("Restore {backup} by upserting verified documents. Current task and backup records are preserved; newer documents are not deleted.", { backup: shortHash(restoreTarget.backup_id, 18) }) : ""} confirmLabel="Restore backup" danger busy={restore.isPending} onConfirm={() => restoreTarget && restore.mutate({ backupId: restoreTarget.backup_id, confirm: true })} />
    </div>
  );
}

function BackupRow({ record, formatBytes, formatDate, t, onRehearse, onRestore }: { record: BackupRecord; formatBytes: (value: number | null | undefined) => string; formatDate: (value: string | null | undefined, withTime?: boolean) => string; t: (key: string, values?: Record<string, string | number>) => string; onRehearse: () => void; onRestore: () => void }) {
  const ready = ["verified", "rehearsed", "restored", "restore_queued"].includes(record.status) && Boolean(record.storage_ref);
  return <tr><td>{formatDate(record.created_at)}</td><td><strong>{shortHash(record.backup_id, 18)}</strong><span className="cell-secondary">{record.format}</span></td><td><StatusBadge status={record.origin} /></td><td>{t(record.scope)}</td><td>{formatBytes(record.size_bytes)}</td><td><StatusBadge status={record.encrypted ? "enabled" : "disabled"} /></td><td>{record.last_rehearsed_at ? formatDate(record.last_rehearsed_at) : <span className="cell-secondary">{t("Not rehearsed")}</span>}</td><td><StatusBadge status={record.status} /></td><td className="mono">{shortHash(record.checksum, 14)}</td><td><div className="row-actions">{ready ? <Button size="sm" variant="ghost" onClick={onRehearse}><CheckCircle2 size={14} /> {t("Verify")}</Button> : null}{ready ? <Button size="sm" variant="ghost" onClick={onRestore}><RotateCcw size={14} /> {t("Restore")}</Button> : null}</div></td></tr>;
}
