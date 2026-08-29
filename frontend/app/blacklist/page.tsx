"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, CirclePlus, Trash2 } from "lucide-react";
import { FormEvent, useState } from "react";
import { createBlacklist, deleteBlacklist, getBlacklist, type DestinationBlacklist } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { formatDate } from "../../lib/utils";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, Panel, StatusBadge } from "../../components/ui";

export default function BlacklistPage() {
  const { t } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [pattern, setPattern] = useState("");
  const [kind, setKind] = useState<DestinationBlacklist["kind"]>("domain");
  const [comment, setComment] = useState("");
  const [removeTarget, setRemoveTarget] = useState<DestinationBlacklist | null>(null);
  const blacklist = useQuery({ queryKey: ["blacklist"], queryFn: getBlacklist, enabled: session === true });
  const create = useMutation({
    mutationFn: () => createBlacklist({ pattern, kind, comment, enabled: true }),
    onSuccess: async () => {
      setPattern(""); setComment(""); setKind("domain"); setShowForm(false);
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["blacklist"] }), queryClient.invalidateQueries({ queryKey: ["sites"] })]);
    },
  });
  const remove = useMutation({
    mutationFn: () => deleteBlacklist(removeTarget!.id),
    onSuccess: async () => {
      setRemoveTarget(null);
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["blacklist"] }), queryClient.invalidateQueries({ queryKey: ["sites"] })]);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pattern.trim()) create.mutate();
  }

  if (session === null) return <LoadingState rows={6} />;
  if (!session) return <SessionGate />;
  if (blacklist.isLoading) return <LoadingState rows={6} />;
  if (blacklist.isError) return <ErrorState error={blacklist.error instanceof Error ? blacklist.error.message : "Unable to load the destination deny list."} onRetry={() => void blacklist.refetch()} />;

  const entries = blacklist.data || [];
  const error = create.error || remove.error;
  return (
    <div className="page-stack">
      <PageHeader eyebrow="POLICY" title="Destination deny" description="Blocked destination patterns are included in every generated bundle." actions={<Button variant="primary" onClick={() => setShowForm((value) => !value)}><CirclePlus size={16} /> {t("Add destination")}</Button>} />
      {error ? <div className="inline-error" role="alert">{error instanceof Error ? error.message : "The destination policy was not accepted."}</div> : null}
      <Panel>
        {showForm ? <form className="form-grid form-grid-blacklist" onSubmit={submit}><label><span>{t("Type")}</span><select value={kind} onChange={(event) => setKind(event.target.value as DestinationBlacklist["kind"])}><option value="domain">{t("Domain")}</option><option value="ip">{t("IP address")}</option><option value="cidr">CIDR</option></select></label><label><span>{t("Pattern")}</span><input autoFocus value={pattern} onChange={(event) => setPattern(event.target.value)} placeholder={kind === "domain" ? "example.com" : kind === "ip" ? "203.0.113.10" : "203.0.113.0/24"} /></label><label><span>{t("Comment")}</span><input value={comment} onChange={(event) => setComment(event.target.value)} placeholder={t("Reason for block")} /></label><div className="form-actions"><Button type="button" onClick={() => setShowForm(false)}>{t("Cancel")}</Button><Button variant="primary" type="submit" disabled={create.isPending}>{create.isPending ? t("Adding...") : t("Add destination")}</Button></div></form> : null}
        <div className="table-wrap"><table><thead><tr><th>{t("Pattern")}</th><th>{t("Type")}</th><th>{t("Comment")}</th><th>{t("Created")}</th><th>{t("State")}</th><th aria-label={t("Actions")} /></tr></thead><tbody>{entries.length ? entries.map((entry) => <tr key={entry.id}><td className="mono">{entry.pattern}</td><td><span className="type-tag">{t(entry.kind)}</span></td><td>{entry.comment || "-"}</td><td>{formatDate(entry.created_at)}</td><td><StatusBadge status={entry.enabled ? "enabled" : "disabled"} /></td><td><button className="row-icon-button row-icon-danger" aria-label={t("Delete {value}", { value: entry.pattern })} title={t("Delete destination")} onClick={() => setRemoveTarget(entry)}><Trash2 size={16} /></button></td></tr>) : <tr><td colSpan={6}><div className="table-empty"><Ban size={18} /> {t("No destinations are blocked.")}</div></td></tr>}</tbody></table></div>
      </Panel>
      <ConfirmDialog open={Boolean(removeTarget)} onOpenChange={(open) => !open && setRemoveTarget(null)} title="Remove destination deny" description={t("Remove {value} from the global deny policy. New releases will no longer include it.", { value: removeTarget?.pattern || t("this pattern") })} confirmLabel="Remove destination" danger busy={remove.isPending} onConfirm={() => remove.mutate()} />
    </div>
  );
}
