"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, Panel, StatusBadge } from "../../components/ui";
import { getNodes, getRelease, getRoutingPolicy, getSites, publishRoutingPolicy, saveRoutingPolicy, type RoutingPolicy, type RoutingPolicyResponse } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { RoutingImport } from "./routing-import";
import styles from "./routing.module.css";

function RoutingEditor({ data, hasNodes }: { data: RoutingPolicyResponse; hasNodes: boolean }) {
  const { t } = usePreferences();
  const client = useQueryClient();
  const [policy, setPolicy] = useState<RoutingPolicy>(data.policy);
  const [confirm, setConfirm] = useState(false);
  const [releaseId, setReleaseId] = useState("");
  const dirty = JSON.stringify(policy) !== JSON.stringify(data.policy);
  const save = useMutation({
    mutationFn: () => saveRoutingPolicy(data.site_id, data.revision, policy),
    onSuccess: (response) => client.setQueryData(["routing", data.site_id], response),
  });
  const publish = useMutation({
    mutationFn: () => publishRoutingPolicy(data.site_id, data.revision),
    onSuccess: (release) => {
      setReleaseId(release.release_id);
      setConfirm(false);
      void client.invalidateQueries({ queryKey: ["nodes"] });
    },
  });
  const release = useQuery({ queryKey: ["routing-release", releaseId], queryFn: () => getRelease(releaseId), enabled: Boolean(releaseId), refetchInterval: 3000 });
  const busy = save.isPending || publish.isPending;
  return <>
    <Panel className={styles.panel}>
      <header className={styles.editorHeader}>
        <div className={styles.heading}>
          <h2>{t("Rule mode")}</h2>
          <span className={styles.revision}>{t("Saved revision")}: {data.revision} {dirty ? t("Unsaved changes") : ""}</span>
        </div>
        <div className={styles.headerActions}>
          <Button data-routing-action="save" disabled={busy || (!dirty && data.revision > 0) || policy.rules.some((rule) => !rule.pattern.trim())} variant="primary" onClick={() => save.mutate()}>{t("Save routing rules")}</Button>
          <Button data-routing-action="publish" disabled={busy || dirty || data.revision === 0 || !hasNodes} onClick={() => setConfirm(true)}>{t("Publish routing rules")}</Button>
        </div>
      </header>
      <fieldset className={styles.fields} disabled={busy} data-routing-site={data.site_id}>
        <div className={styles.routeControls}>
          <label className={styles.checkOption}><input type="checkbox" checked={policy.private_direct} onChange={(event) => setPolicy({ ...policy, private_direct: event.target.checked })} /> {t("Private addresses direct")}</label>
          <label className={styles.checkOption}><input type="checkbox" checked={policy.cn_direct} onChange={(event) => setPolicy({ ...policy, cn_direct: event.target.checked })} /> {t("China domains and IPs direct")}</label>
          <div className={styles.radioControl}>
            <span id="routing-fallback-label">{t("Unmatched destinations")}</span>
            <div className={styles.radioGroup} role="radiogroup" aria-labelledby="routing-fallback-label">
              {(["direct", "proxy"] as const).map((outbound) => <label className={styles.radioOption} key={outbound}>
                <input type="radio" name="routing-final" value={outbound} checked={policy.final === outbound} onChange={() => setPolicy({ ...policy, final: outbound })} />
                {t(outbound === "direct" ? "Direct outbound" : "Proxy outbound")}
              </label>)}
            </div>
          </div>
        </div>
        <p>{t("Access restrictions run first. Custom rules match from top to bottom, followed by private and China direct rules, then the fallback.")}</p>
        <p>{t("Saving changes does not publish them. Publish the saved revision to all nodes in this site and inspect the release acknowledgements.")}</p>
        <RoutingImport policy={policy} onApply={setPolicy} />
        {policy.rules.length > 0 && <details className={styles.currentRules}>
          <summary>{t("View current routing rules")} ({policy.rules.length})</summary>
          <pre data-routing-current-rules>{JSON.stringify(policy.rules, null, 2)}</pre>
        </details>}
      </fieldset>
      {(save.error || publish.error) && <p role="alert">{t((save.error || publish.error)?.message || "Operation failed")}</p>}
      {releaseId && <p role="status"><Link href="/releases">{t("Routing release")}: {releaseId}</Link> <StatusBadge status={release.data?.status || "pending"} /> {release.error?.message}</p>}
    </Panel>
    <ConfirmDialog open={confirm} onOpenChange={setConfirm} title="Publish routing rules" description={t("Apply the saved routing rules to every node in this site. Nodes validate the signed bundle and roll back if the health check fails.")} confirmLabel="Publish routing rules" busy={publish.isPending} onConfirm={() => publish.mutate()} />
  </>;
}

export default function RoutingPage() {
  const { t } = usePreferences();
  const session = useManagementSession();
  const [selectedSite, setSelectedSite] = useState("");
  const sites = useQuery({ queryKey: ["sites"], queryFn: getSites, enabled: session === true });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, refetchInterval: 5000 });
  const siteId = selectedSite || nodes.data?.[0]?.site_id || sites.data?.[0]?.id || "";
  const routing = useQuery({ queryKey: ["routing", siteId], queryFn: () => getRoutingPolicy(siteId), enabled: session === true && Boolean(siteId) });
  if (session === null) return <LoadingState rows={5} />;
  if (!session) return <SessionGate />;
  if (sites.isError || routing.isError || nodes.isError) return <ErrorState error={t("Unable to load routing policy")} onRetry={() => { void sites.refetch(); void routing.refetch(); void nodes.refetch(); }} />;
  return <div className="page-stack">
    <PageHeader title="Routing rules" description="Rule mode routes each destination using ordered rules instead of sending all traffic through the proxy." />
    <Panel className={styles.panel}>
      <div className={styles.radioControl}>
        <span id="routing-site-label">{t("Sites")}</span>
        <div className={styles.radioGroup} role="radiogroup" aria-labelledby="routing-site-label">
          {sites.data?.map((site) => <label className={styles.radioOption} key={site.id}>
            <input type="radio" name="routing-site" value={site.id} checked={siteId === site.id} onChange={() => setSelectedSite(site.id)} />
            {site.name}
          </label>)}
        </div>
      </div>
    </Panel>
    {routing.data ? <RoutingEditor key={`${siteId}:${routing.data.revision}`} data={routing.data} hasNodes={Boolean(nodes.data?.some((node) => node.site_id === siteId))} /> : sites.isLoading || siteId ? <LoadingState rows={4} /> : <p>{t("No sites available")}</p>}
  </div>;
}
