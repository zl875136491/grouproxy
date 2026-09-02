"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { BadgeCheck, Braces, Download, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { getAudit, getAuditExport, verifyAudit, type AuditEvent } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { shortHash } from "../../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { ListFilters, timeRangeStart, type TimeRange } from "../../components/list-filters";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, DetailDialog, Panel, StatusBadge } from "../../components/ui";

export default function AuditPage() {
  const { t, formatDate, formatNumber } = usePreferences();
  const session = useManagementSession();
  const [selected, setSelected] = useState<AuditEvent | null>(null);
  const [exporting, setExporting] = useState(false);
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [timeRange, setTimeRange] = useState<TimeRange>("30d");
  const since = useMemo(() => timeRangeStart(timeRange), [timeRange]);
  const audit = useQuery({ queryKey: ["audit", action, actor, since], queryFn: () => getAudit({ action: action || undefined, actor: actor.trim() || undefined, since }), enabled: session === true });
  const verify = useMutation({ mutationFn: verifyAudit });

  async function downloadExport(exportFormat: "json" | "ndjson") {
    setExporting(true);
    try {
      const content = await getAuditExport(exportFormat);
      const link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob([content], { type: exportFormat === "json" ? "application/json" : "application/x-ndjson" }));
      link.download = `grouproxy-audit.${exportFormat}`;
      link.click();
      URL.revokeObjectURL(link.href);
    } finally {
      setExporting(false);
    }
  }

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (audit.isLoading) return <LoadingState rows={7} />;
  if (audit.isError) return <ErrorState error={audit.error instanceof Error ? audit.error.message : "Unable to load audit events."} onRetry={() => void audit.refetch()} />;

  const events = audit.data || [];
  return (
    <div className="page-stack">
      <PageHeader eyebrow="GOVERN" title="Audit" description="Append-only control-plane events with a verifiable hash chain." actions={<div className="page-action-group"><Button onClick={() => void downloadExport("json")} disabled={exporting}><Download size={16} /> {t("Export JSON")}</Button><Button onClick={() => void downloadExport("ndjson")} disabled={exporting}><Download size={16} /> {t("Export NDJSON")}</Button><Button variant="primary" onClick={() => verify.mutate()} disabled={verify.isPending}><ShieldCheck size={16} /> {verify.isPending ? t("Verifying...") : t("Verify chain")}</Button></div>} />
      {verify.data ? <div className={`alert-strip ${verify.data.valid ? "alert-success" : "alert-danger"}`}><BadgeCheck size={18} /><div><strong>{t(verify.data.valid ? "Audit chain is valid" : "Audit chain verification failed")}</strong><span>{t("{count} events checked", { count: formatNumber(verify.data.event_count) })}{verify.data.error ? ` · ${verify.data.error}` : ""}</span></div></div> : null}
      {verify.error ? <div className="inline-error" role="alert">{verify.error instanceof Error ? verify.error.message : "Audit verification failed."}</div> : null}
      <Panel>
        <div className="table-toolbar"><div className="toolbar-title"><Braces size={18} /><span>{t("{count} events", { count: formatNumber(events.length) })}</span></div><span className="toolbar-note">{t("Sensitive values are redacted before storage.")}</span></div>
        <ListFilters search={actor} setSearch={setActor} searchPlaceholder="Filter by actor" timeRange={timeRange} setTimeRange={setTimeRange} selects={[{ label: "Action", value: action, setValue: setAction, options: [{ value: "", label: "All actions" }, { value: "config_release.create", label: "config release" }, { value: "config_draft.create", label: "config draft" }, { value: "proxy_selection.update", label: "proxy selection" }, { value: "node.rename", label: "node rename" }] }]} />
        {events.length ? <div className="table-wrap"><table><thead><tr><th>{t("Time")}</th><th>{t("Action")}</th><th>{t("Target")}</th><th>{t("Actor")}</th><th>{t("Result")}</th><th>{t("Hash")}</th><th aria-label={t("Details")} /></tr></thead><tbody>{events.map((event) => <tr key={event.event_id}><td>{formatDate(event.at)}</td><td><strong>{event.action}</strong></td><td>{event.target_type}<span className="cell-secondary mono">{shortHash(event.target_id, 14)}</span></td><td>{event.actor}</td><td><StatusBadge status={event.result} /></td><td className="mono">{shortHash(event.immutable_hash, 14)}</td><td><Button variant="ghost" size="sm" onClick={() => setSelected(event)}>{t("View")}</Button></td></tr>)}</tbody></table></div> : <EmptyState title="No audit events recorded" />}
      </Panel>
      <DetailDialog open={Boolean(selected)} onOpenChange={(open) => !open && setSelected(null)} title={selected?.action || "Audit event"} description={selected ? `${formatDate(selected.at)} · ${selected.actor}` : undefined}>{selected ? <div className="detail-stack"><dl className="detail-list"><div><dt>{t("Target")}</dt><dd>{selected.target_type} · <span className="mono">{selected.target_id}</span></dd></div><div><dt>{t("Request")}</dt><dd className="mono">{shortHash(selected.request_id, 20)}</dd></div><div><dt>{t("Immutable hash")}</dt><dd className="mono">{selected.immutable_hash}</dd></div><div><dt>{t("Previous hash")}</dt><dd className="mono">{selected.previous_hash || t("Chain origin")}</dd></div></dl><div className="audit-payload"><strong>{t("Before")}</strong><pre>{JSON.stringify(selected.before, null, 2)}</pre></div><div className="audit-payload"><strong>{t("After")}</strong><pre>{JSON.stringify(selected.after, null, 2)}</pre></div>{selected.error ? <div className="detail-error"><strong>{t("Error")}</strong><code>{selected.error}</code></div> : null}</div> : null}</DetailDialog>
    </div>
  );
}
