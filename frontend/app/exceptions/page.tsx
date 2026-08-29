"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, CirclePlus, Trash2 } from "lucide-react";
import { FormEvent, useMemo, useState } from "react";
import { createException, deleteException, getExceptions, type TravelException } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { formatDate, formatDurationUntil } from "../../lib/utils";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, Panel, StatusBadge } from "../../components/ui";

function defaultExpiry() {
  const date = new Date(Date.now() + 8 * 60 * 60 * 1000);
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
}

export default function ExceptionsPage() {
  const { t } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [cidr, setCIDR] = useState("");
  const [owner, setOwner] = useState("");
  const [comment, setComment] = useState("");
  const [expiresAt, setExpiresAt] = useState(defaultExpiry);
  const [removeTarget, setRemoveTarget] = useState<TravelException | null>(null);
  const exceptions = useQuery({ queryKey: ["exceptions"], queryFn: getExceptions, enabled: session === true, refetchInterval: 30_000 });
  const create = useMutation({
    mutationFn: () => createException({ cidr, owner, comment, expires_at: new Date(expiresAt).toISOString(), enabled: true }),
    onSuccess: async () => {
      setCIDR(""); setOwner(""); setComment(""); setExpiresAt(defaultExpiry()); setShowForm(false);
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["exceptions"] }), queryClient.invalidateQueries({ queryKey: ["sites"] })]);
    },
  });
  const remove = useMutation({
    mutationFn: () => deleteException(removeTarget!.id),
    onSuccess: async () => {
      setRemoveTarget(null);
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["exceptions"] }), queryClient.invalidateQueries({ queryKey: ["sites"] })]);
    },
  });
  const sorted = useMemo(() => [...(exceptions.data || [])].sort((a, b) => a.expires_at.localeCompare(b.expires_at)), [exceptions.data]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (cidr.trim() && owner.trim() && expiresAt) create.mutate();
  }

  if (session === null) return <LoadingState rows={6} />;
  if (!session) return <SessionGate />;
  if (exceptions.isLoading) return <LoadingState rows={6} />;
  if (exceptions.isError) return <ErrorState error={exceptions.error instanceof Error ? exceptions.error.message : "Unable to load exceptions."} onRetry={() => void exceptions.refetch()} />;

  const error = create.error || remove.error;
  return (
    <div className="page-stack">
      <PageHeader eyebrow="POLICY" title="Travel exceptions" description="Temporary source access applies to all sites and expires automatically." actions={<Button variant="primary" onClick={() => setShowForm((value) => !value)}><CirclePlus size={16} /> {t("Add exception")}</Button>} />
      {error ? <div className="inline-error" role="alert">{error instanceof Error ? error.message : "The exception was not accepted."}</div> : null}
      <Panel>
        {showForm ? <form className="form-grid form-grid-exception" onSubmit={submit}><label><span>{t("CIDR or IP")}</span><input autoFocus value={cidr} onChange={(event) => setCIDR(event.target.value)} placeholder="198.51.100.18/32" /></label><label><span>{t("Owner")}</span><input value={owner} onChange={(event) => setOwner(event.target.value)} placeholder={t("Operator name")} /></label><label><span>{t("Expires at")}</span><input type="datetime-local" value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} /></label><label><span>{t("Comment")}</span><input value={comment} onChange={(event) => setComment(event.target.value)} placeholder={t("Travel approval reference")} /></label><div className="form-actions"><Button type="button" onClick={() => setShowForm(false)}>{t("Cancel")}</Button><Button variant="primary" type="submit" disabled={create.isPending}>{create.isPending ? t("Adding...") : t("Add exception")}</Button></div></form> : null}
        <div className="table-wrap"><table><thead><tr><th>CIDR</th><th>{t("Owner")}</th><th>{t("Expires")}</th><th>{t("State")}</th><th>{t("Comment")}</th><th aria-label={t("Actions")} /></tr></thead><tbody>{sorted.length ? sorted.map((item) => { const expired = new Date(item.expires_at).getTime() <= Date.now(); return <tr key={item.id}><td className="mono">{item.cidr}</td><td>{item.owner || "-"}</td><td><strong>{formatDurationUntil(item.expires_at)}</strong><span className="cell-secondary">{formatDate(item.expires_at)}</span></td><td><StatusBadge status={expired ? "expired" : item.enabled ? "enabled" : "disabled"} /></td><td>{item.comment || "-"}</td><td><button className="row-icon-button row-icon-danger" aria-label={t("Delete {value}", { value: item.cidr })} title={t("Delete exception")} onClick={() => setRemoveTarget(item)}><Trash2 size={16} /></button></td></tr>; }) : <tr><td colSpan={6}><div className="table-empty"><CalendarClock size={18} /> {t("No active exceptions.")}</div></td></tr>}</tbody></table></div>
      </Panel>
      <ConfirmDialog open={Boolean(removeTarget)} onOpenChange={(open) => !open && setRemoveTarget(null)} title="Remove travel exception" description={t("Remove {value}. The next release for each site will exclude it from effective source access.", { value: removeTarget?.cidr || t("this exception") })} confirmLabel="Remove exception" danger busy={remove.isPending} onConfirm={() => remove.mutate()} />
    </div>
  );
}
