"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, CirclePlus, Trash2 } from "lucide-react";
import { FormEvent, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { createSourceBlacklist, deleteSourceBlacklist, getSites, getSourceBlacklist, type SourceBlacklist } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, DetailDialog, Panel, StatusBadge } from "../../components/ui";

export default function BlacklistPage() {
  const { t, formatDate } = usePreferences();
  const searchParams = useSearchParams();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [scope, setScope] = useState<SourceBlacklist["scope"]>("global");
  const [siteId, setSiteId] = useState("");
  const [kind, setKind] = useState<SourceBlacklist["kind"]>("domain");
  const [pattern, setPattern] = useState("");
  const [comment, setComment] = useState("");
  const [removeTarget, setRemoveTarget] = useState<SourceBlacklist | null>(null);
  const initializedFromSearch = useRef(false);
  const blacklist = useQuery({ queryKey: ["source-blacklist"], queryFn: getSourceBlacklist, enabled: session === true });
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  useEffect(() => {
    if (initializedFromSearch.current || !sites.data) return;
    initializedFromSearch.current = true;
    const requestedSiteId = searchParams.get("site");
    if (searchParams.get("scope") === "site" && requestedSiteId && sites.data.some((site) => site.id === requestedSiteId)) {
      setScope("site");
      setSiteId(requestedSiteId);
    }
  }, [searchParams, sites.data]);
  const create = useMutation({
    mutationFn: () => createSourceBlacklist({ scope, site_id: scope === "site" ? siteId : null, kind, pattern: pattern.trim(), comment: comment.trim(), enabled: true }),
    onSuccess: async (mutation) => { queryClient.setQueryData<SourceBlacklist[]>(["source-blacklist"], (current = []) => [...current.filter((entry) => entry.id !== mutation.rule.id), mutation.rule]); setPattern(""); setComment(""); setScope("global"); setSiteId(""); setKind("domain"); setShowForm(false); await Promise.all([queryClient.invalidateQueries({ queryKey: ["source-blacklist"] }), queryClient.invalidateQueries({ queryKey: ["sites"] })]); },
  });
  const remove = useMutation({
    mutationFn: () => deleteSourceBlacklist(removeTarget!.id),
    onSuccess: async (mutation) => { queryClient.setQueryData<SourceBlacklist[]>(["source-blacklist"], (current = []) => current.filter((entry) => entry.id !== mutation.rule.id)); setRemoveTarget(null); await Promise.all([queryClient.invalidateQueries({ queryKey: ["source-blacklist"] }), queryClient.invalidateQueries({ queryKey: ["sites"] })]); },
  });
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (pattern.trim() && (scope === "global" || siteId)) create.mutate(); }
  if (session === null) return <LoadingState rows={6} />;
  if (!session) return <SessionGate />;
  if (blacklist.isLoading || sites.isLoading) return <LoadingState rows={6} />;
  if (blacklist.isError || sites.isError) { const issue = blacklist.error || sites.error; return <ErrorState error={issue instanceof Error ? issue.message : t("Unable to load source blacklist.")} onRetry={() => void Promise.all([blacklist.refetch(), sites.refetch()])} />; }
  const entries = blacklist.data || [];
  const siteNames = new Map((sites.data || []).map((site) => [site.id, site.name]));
  const placeholder = kind === "domain" ? "example.com" : kind === "ip" ? "203.0.113.10" : "203.0.113.0/24";
  return (
    <div className="page-stack page-fill list-page">
      <PageHeader eyebrow="POLICY" title={t("Source blacklist")} description={t("Requests are allowed by default. Add an explicit source IP, network, or domain rule only when it should be blocked.")} actions={<Button variant="primary" onClick={() => { create.reset(); setShowForm(true); }}><CirclePlus size={16} /> {t("Add source rule")}</Button>} />
      <Panel className="list-panel">
        <div className="table-toolbar"><div className="toolbar-title"><Ban size={18} /><span>{t("{count} source rules", { count: entries.length })}</span></div><span className="toolbar-note">{t("Global rules apply to every site; site rules apply only to the selected site.")}</span></div>
        <div className="table-wrap table-scroll"><table><thead><tr><th>{t("Scope")}</th><th>{t("Site")}</th><th>{t("Type")}</th><th>{t("Pattern")}</th><th>{t("Comment")}</th><th>{t("Created")}</th><th>{t("State")}</th><th aria-label={t("Actions")} /></tr></thead><tbody>{entries.length ? entries.map((entry) => <tr key={entry.id}><td><span className="type-tag">{t(entry.scope === "global" ? "Global" : "Site")}</span></td><td>{entry.scope === "global" ? t("All sites") : t(siteNames.get(entry.site_id || "") || "Unknown site")}</td><td><span className="type-tag">{t(entry.kind)}</span></td><td className="mono">{entry.pattern}</td><td>{entry.comment || "-"}</td><td>{formatDate(entry.created_at)}</td><td><StatusBadge status={entry.enabled ? "enabled" : "disabled"} /></td><td><button className="row-icon-button row-icon-danger" aria-label={t("Delete {value}", { value: entry.pattern })} title={t("Delete source rule")} onClick={() => setRemoveTarget(entry)}><Trash2 size={16} /></button></td></tr>) : <tr><td colSpan={8}><div className="table-empty"><Ban size={18} /> {t("No source rules are blocking requests.")}</div></td></tr>}</tbody></table></div>
      </Panel>
      <DetailDialog open={showForm} onOpenChange={(open) => { if (!open && !create.isPending) setShowForm(false); }} title={t("Add source rule")} description={t("Block a source IP, network, or domain globally or for one site.")} contentClassName="policy-form-dialog">
        <form className="policy-dialog-form" onSubmit={submit}>
          <label><span>{t("Scope")}</span><select value={scope} onChange={(event) => { const value = event.target.value as SourceBlacklist["scope"]; setScope(value); if (value === "global") setSiteId(""); }}><option value="global">{t("Global")}</option><option value="site">{t("Site")}</option></select></label>
          {scope === "site" ? <label><span>{t("Site")}</span><select required value={siteId} onChange={(event) => setSiteId(event.target.value)}><option value="">{t("Select a site")}</option>{(sites.data || []).map((site) => <option key={site.id} value={site.id}>{t(site.name)}</option>)}</select></label> : null}
          <label><span>{t("Type")}</span><select value={kind} onChange={(event) => setKind(event.target.value as SourceBlacklist["kind"])}><option value="domain">{t("Domain")}</option><option value="ip">{t("IP address")}</option><option value="network">{t("Network")}</option></select></label>
          <label><span>{t("Pattern")}</span><input autoFocus value={pattern} onChange={(event) => setPattern(event.target.value)} placeholder={placeholder} /></label>
          <label><span>{t("Comment")}</span><input value={comment} onChange={(event) => setComment(event.target.value)} placeholder={t("Reason for block")} /></label>
          <div className="form-actions"><Button type="button" onClick={() => setShowForm(false)} disabled={create.isPending}>{t("Cancel")}</Button><Button variant="primary" type="submit" disabled={create.isPending || !pattern.trim() || (scope === "site" && !siteId)}>{create.isPending ? t("Adding...") : t("Add source rule")}</Button></div>
        </form>
      </DetailDialog>
      <ConfirmDialog open={Boolean(removeTarget)} onOpenChange={(open) => !open && setRemoveTarget(null)} title={t("Remove source rule")} description={t("Remove {value} from the source blacklist. The change is released automatically.", { value: removeTarget?.pattern || t("this pattern") })} confirmLabel={t("Remove source rule")} danger busy={remove.isPending} onConfirm={() => remove.mutate()} />
    </div>
  );
}
