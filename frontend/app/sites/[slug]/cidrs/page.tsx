"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CirclePlus, FileDiff, Network, Search, Trash2 } from "lucide-react";
import { useParams, useRouter } from "next/navigation";
import { FormEvent, useMemo, useState } from "react";
import {
  addCIDR,
  createDraft,
  deleteCIDR,
  getSiteCIDRs,
  getSites,
  previewCIDR,
  type CIDREntry,
} from "../../../../lib/api";
import { usePreferences } from "../../../../lib/preferences";
import { ErrorState, LoadingState } from "../../../../components/data-state";
import { PageHeader } from "../../../../components/page-header";
import { SessionGate, useManagementSession } from "../../../../components/session-gate";
import { Button, ConfirmDialog, Panel, StatusBadge } from "../../../../components/ui";

type Change = { action: "added" | "removed"; cidr: string };

export default function SiteCIDRPage() {
  const { t } = usePreferences();
  const params = useParams<{ slug: string }>();
  const router = useRouter();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);
  const [cidr, setCIDR] = useState("");
  const [comment, setComment] = useState("");
  const [sourceIP, setSourceIP] = useState("");
  const [removeTarget, setRemoveTarget] = useState<CIDREntry | null>(null);
  const [changes, setChanges] = useState<Change[]>([]);
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  const site = useMemo(() => (sites.data || []).find((item) => item.slug === params.slug), [params.slug, sites.data]);
  const cidrs = useQuery({ queryKey: ["cidrs", site?.id], queryFn: () => getSiteCIDRs(site!.id), enabled: Boolean(site) });
  const accessPreview = useMutation({ mutationFn: () => previewCIDR(site!.id, sourceIP) });
  const add = useMutation({
    mutationFn: () => addCIDR(site!.id, { cidr, comment, enabled: true }),
    onSuccess: async (entry) => {
      setChanges((current) => [...current, { action: "added", cidr: entry.cidr }]);
      setCIDR("");
      setComment("");
      setShowAdd(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["cidrs", site?.id] }),
        queryClient.invalidateQueries({ queryKey: ["sites"] }),
      ]);
    },
  });
  const remove = useMutation({
    mutationFn: () => deleteCIDR(site!.id, removeTarget!.id),
    onSuccess: async () => {
      if (removeTarget) setChanges((current) => [...current, { action: "removed", cidr: removeTarget.cidr }]);
      setRemoveTarget(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["cidrs", site?.id] }),
        queryClient.invalidateQueries({ queryKey: ["sites"] }),
      ]);
    },
  });
  const draft = useMutation({
    mutationFn: () => createDraft({
      site_id: site!.id,
      diff: {
        source_access: {
          effective_cidrs: (cidrs.data || []).filter((entry) => entry.enabled).map((entry) => entry.cidr),
          changes,
        },
      },
      note: "CIDR policy update",
    }),
    onSuccess: async (item) => {
      setChanges([]);
      await queryClient.invalidateQueries({ queryKey: ["drafts"] });
      router.push(`/releases?draft=${item.id}`);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (cidr.trim()) add.mutate();
  }

  if (session === null) return <LoadingState rows={6} />;
  if (!session) return <SessionGate />;
  if (sites.isLoading) return <LoadingState rows={6} />;
  if (sites.isError) return <ErrorState error={sites.error instanceof Error ? sites.error.message : "Unable to load sites."} onRetry={() => void sites.refetch()} />;
  if (!site) return <ErrorState error="This site does not exist." />;
  if (cidrs.isLoading) return <LoadingState rows={6} />;
  if (cidrs.isError) return <ErrorState error={cidrs.error instanceof Error ? cidrs.error.message : "Unable to load CIDRs."} onRetry={() => void cidrs.refetch()} />;

  const cidrItems = cidrs.data || [];
  const preview = accessPreview.data;
  const mutationError = add.error || remove.error || draft.error;

  return (
    <div className="page-stack">
      <PageHeader eyebrow="SITE POLICY" title={t("{name} CIDRs", { name: t(site.name) })} description={t("Policy revision v{revision} · HTTP :{port}", { revision: site.config_revision, port: site.http_port })} actions={<Button variant="primary" onClick={() => draft.mutate()} disabled={draft.isPending}><FileDiff size={16} /> {t("Create draft")}</Button>} />
      {mutationError ? <div className="inline-error" role="alert">{mutationError instanceof Error ? mutationError.message : "The policy change was not accepted."}</div> : null}
      <section className="policy-grid">
        <Panel>
          <div className="panel-heading"><div><span className="panel-kicker">{t("SOURCE ACCESS")}</span><h2>{t("Site CIDRs")}</h2></div><Button size="sm" variant="secondary" onClick={() => setShowAdd((value) => !value)}><CirclePlus size={15} /> {t("Add CIDR")}</Button></div>
          {showAdd ? <form className="inline-form" onSubmit={submit}><label><span>CIDR</span><input autoFocus value={cidr} onChange={(event) => setCIDR(event.target.value)} placeholder="10.32.12.0/24" /></label><label><span>{t("Comment")}</span><input value={comment} onChange={(event) => setComment(event.target.value)} placeholder={t("Office network")} /></label><Button variant="primary" type="submit" disabled={add.isPending}>{add.isPending ? t("Adding...") : t("Add")}</Button></form> : null}
          <div className="table-wrap"><table><thead><tr><th>CIDR</th><th>{t("Comment")}</th><th>{t("State")}</th><th aria-label={t("Actions")} /></tr></thead><tbody>{cidrItems.length ? cidrItems.map((entry) => <tr key={entry.id}><td className="mono">{entry.cidr}</td><td>{entry.comment || "-"}</td><td><StatusBadge status={entry.enabled ? "enabled" : "disabled"} /></td><td><button className="row-icon-button row-icon-danger" aria-label={t("Remove {value}", { value: entry.cidr })} title={t("Remove CIDR")} onClick={() => setRemoveTarget(entry)}><Trash2 size={16} /></button></td></tr>) : <tr><td colSpan={4}><div className="table-empty">{t("No site CIDRs configured.")}</div></td></tr>}</tbody></table></div>
          {changes.length ? <div className="change-summary"><FileDiff size={16} /><span>{t("{count} local policy changes included in the next draft.", { count: changes.length })}</span></div> : null}
        </Panel>
        <Panel>
          <div className="panel-heading"><div><span className="panel-kicker">{t("POLICY TEST")}</span><h2>{t("Source IP preview")}</h2></div><Network size={18} /></div>
          <form className="preview-form" onSubmit={(event) => { event.preventDefault(); if (sourceIP.trim()) accessPreview.mutate(); }}><label><span>{t("Source IP")}</span><input value={sourceIP} onChange={(event) => setSourceIP(event.target.value)} placeholder="10.32.12.111" /></label><Button type="submit" variant="secondary" disabled={accessPreview.isPending}><Search size={15} /> {accessPreview.isPending ? t("Checking...") : t("Check access")}</Button></form>
          {accessPreview.error ? <div className="inline-error">{accessPreview.error instanceof Error ? t(accessPreview.error.message) : t("Invalid source IP.")}</div> : null}
          {preview ? <div className={`preview-result ${preview.allowed ? "preview-allow" : "preview-deny"}`}><div><StatusBadge status={preview.allowed ? "allowed" : "denied"} /><strong>{preview.allowed ? preview.matched_cidr : t(preview.reason.replaceAll("_", " "))}</strong></div><span>{preview.requires_auth ? t("Site authentication is also required.") : t("CIDR is the current access boundary.")}</span><code>{preview.effective_cidrs.join("\n") || t("No effective CIDRs")}</code></div> : <div className="quiet-placeholder"><CheckCircle2 size={18} /> {t("Enter an address to calculate the effective access policy.")}</div>}
        </Panel>
      </section>
      <ConfirmDialog open={Boolean(removeTarget)} onOpenChange={(open) => !open && setRemoveTarget(null)} title="Remove site CIDR" description={t("Remove {cidr} from {site}. The change remains local to the control plane until a release succeeds.", { cidr: removeTarget?.cidr || t("this CIDR"), site: t(site.name) })} confirmLabel="Remove CIDR" danger busy={remove.isPending} onConfirm={() => remove.mutate()} />
    </div>
  );
}
