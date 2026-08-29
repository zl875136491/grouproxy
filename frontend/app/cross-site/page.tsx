"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRightLeft } from "lucide-react";
import { useMemo, useState } from "react";
import { getCrossSiteAllows, getSites, saveCrossSiteAllow, type Site } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { ConfirmDialog, Panel } from "../../components/ui";

type PendingChange = { from: Site; to: Site; enabled: boolean };

export default function CrossSitePage() {
  const { t } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<PendingChange | null>(null);
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true, staleTime: 30_000 });
  const allows = useQuery({ queryKey: ["cross-site-allows"], queryFn: getCrossSiteAllows, enabled: session === true });
  const update = useMutation({
    mutationFn: (value: PendingChange) => saveCrossSiteAllow({ from_site_id: value.from.id, to_site_id: value.to.id, enabled: value.enabled, comment: "" }),
    onSuccess: async () => {
      setPending(null);
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["cross-site-allows"] }), queryClient.invalidateQueries({ queryKey: ["sites"] })]);
    },
  });
  const matrix = useMemo(() => new Map((allows.data || []).map((item) => [`${item.from_site_id}:${item.to_site_id}`, item])), [allows.data]);

  function setAccess(from: Site, to: Site, enabled: boolean) {
    const value = { from, to, enabled };
    if (enabled) setPending(value);
    else update.mutate(value);
  }

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (sites.isLoading || allows.isLoading) return <LoadingState rows={7} />;
  if (sites.isError || allows.isError) {
    const issue = sites.error || allows.error;
    return <ErrorState error={issue instanceof Error ? issue.message : "Unable to load cross-site access."} onRetry={() => void Promise.all([sites.refetch(), allows.refetch()])} />;
  }

  const siteItems = sites.data || [];
  const error = update.error;
  return (
    <div className="page-stack">
      <PageHeader eyebrow="POLICY" title="Cross-site access" description="Source CIDRs from one site can be added to another site only through an explicit allow." />
      {error ? <div className="inline-error" role="alert">{error instanceof Error ? error.message : "The cross-site policy was not accepted."}</div> : null}
      <Panel>
        <div className="panel-heading"><div><span className="panel-kicker">{t("DEFAULT DENY")}</span><h2>{t("Source-to-destination matrix")}</h2></div><ArrowRightLeft size={19} /></div>
        <div className="matrix-legend"><span>{t("Row: source site")}</span><span>{t("Column: destination listener")}</span></div>
        <div className="matrix-wrap"><table className="policy-matrix"><thead><tr><th>{t("From \\ To")}</th>{siteItems.map((site) => <th key={site.id}>{t(site.name)}</th>)}</tr></thead><tbody>{siteItems.map((from) => <tr key={from.id}><th>{t(from.name)}</th>{siteItems.map((to) => { if (from.id === to.id) return <td className="matrix-self" key={to.id}>-</td>; const allowed = matrix.get(`${from.id}:${to.id}`)?.enabled || false; const isUpdating = update.isPending && update.variables?.from.id === from.id && update.variables?.to.id === to.id; return <td key={to.id}><label className="matrix-toggle"><input type="checkbox" checked={allowed} disabled={isUpdating} onChange={(event) => setAccess(from, to, event.target.checked)} /><span aria-hidden="true" /><span className="sr-only">{t("Allow {from} CIDRs to access {to}", { from: t(from.name), to: t(to.name) })}</span></label></td>; })}</tr>)}</tbody></table></div>
      </Panel>
      <ConfirmDialog open={Boolean(pending)} onOpenChange={(open) => !open && setPending(null)} title="Enable cross-site access" description={pending ? t("Add {from} source CIDRs to the effective access policy for {to}. This expands the destination listener's allowed network scope.", { from: t(pending.from.name), to: t(pending.to.name) }) : ""} confirmLabel="Enable access" busy={update.isPending} onConfirm={() => pending && update.mutate(pending)} />
    </div>
  );
}
