"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { BadgeCheck, Braces, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { getAudit, verifyAudit, type AuditEvent } from "../../lib/api";
import { formatDate, shortHash } from "../../lib/utils";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, DetailDialog, Panel, StatusBadge } from "../../components/ui";

export default function AuditPage() {
  const session = useManagementSession();
  const [selected, setSelected] = useState<AuditEvent | null>(null);
  const audit = useQuery({ queryKey: ["audit"], queryFn: getAudit, enabled: session === true });
  const verify = useMutation({ mutationFn: verifyAudit });

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (audit.isLoading) return <LoadingState rows={7} />;
  if (audit.isError) return <ErrorState error={audit.error instanceof Error ? audit.error.message : "Unable to load audit events."} onRetry={() => void audit.refetch()} />;

  const events = audit.data || [];
  return (
    <div className="page-stack">
      <PageHeader eyebrow="GOVERN" title="Audit" description="Append-only control-plane events with a verifiable hash chain." actions={<Button variant="primary" onClick={() => verify.mutate()} disabled={verify.isPending}><ShieldCheck size={16} /> {verify.isPending ? "Verifying..." : "Verify chain"}</Button>} />
      {verify.data ? <div className={`alert-strip ${verify.data.valid ? "alert-success" : "alert-danger"}`}><BadgeCheck size={18} /><div><strong>{verify.data.valid ? "Audit chain is valid" : "Audit chain verification failed"}</strong><span>{verify.data.event_count} events checked{verify.data.error ? ` · ${verify.data.error}` : ""}</span></div></div> : null}
      {verify.error ? <div className="inline-error" role="alert">{verify.error instanceof Error ? verify.error.message : "Audit verification failed."}</div> : null}
      <Panel>
        <div className="table-toolbar"><div className="toolbar-title"><Braces size={18} /><span>{events.length} events</span></div><span className="toolbar-note">Sensitive values are redacted before storage.</span></div>
        {events.length ? <div className="table-wrap"><table><thead><tr><th>Time</th><th>Action</th><th>Target</th><th>Actor</th><th>Result</th><th>Hash</th><th aria-label="Details" /></tr></thead><tbody>{events.map((event) => <tr key={event.event_id}><td>{formatDate(event.at)}</td><td><strong>{event.action}</strong></td><td>{event.target_type}<span className="cell-secondary mono">{shortHash(event.target_id, 14)}</span></td><td>{event.actor}</td><td><StatusBadge status={event.result} /></td><td className="mono">{shortHash(event.immutable_hash, 14)}</td><td><Button variant="ghost" size="sm" onClick={() => setSelected(event)}>View</Button></td></tr>)}</tbody></table></div> : <EmptyState title="No audit events recorded" />}
      </Panel>
      <DetailDialog open={Boolean(selected)} onOpenChange={(open) => !open && setSelected(null)} title={selected?.action || "Audit event"} description={selected ? `${formatDate(selected.at)} · ${selected.actor}` : undefined}>{selected ? <div className="detail-stack"><dl className="detail-list"><div><dt>Target</dt><dd>{selected.target_type} · <span className="mono">{selected.target_id}</span></dd></div><div><dt>Request</dt><dd className="mono">{shortHash(selected.request_id, 20)}</dd></div><div><dt>Immutable hash</dt><dd className="mono">{selected.immutable_hash}</dd></div><div><dt>Previous hash</dt><dd className="mono">{selected.previous_hash || "Chain origin"}</dd></div></dl><div className="audit-payload"><strong>Before</strong><pre>{JSON.stringify(selected.before, null, 2)}</pre></div><div className="audit-payload"><strong>After</strong><pre>{JSON.stringify(selected.after, null, 2)}</pre></div>{selected.error ? <div className="detail-error"><strong>Error</strong><code>{selected.error}</code></div> : null}</div> : null}</DetailDialog>
    </div>
  );
}
