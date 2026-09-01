"use client";

import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { getAlerts, getNodes, getSites, type Alert } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { IconButton, Panel, StatusBadge } from "../../components/ui";

type AlertFilter = "open" | "resolved" | "";

export default function AlertsPage() {
  const { formatDate, formatNumber, t } = usePreferences();
  const session = useManagementSession();
  const [status, setStatus] = useState<AlertFilter>("open");
  const alerts = useQuery({
    queryKey: ["alerts", status],
    queryFn: () => getAlerts(status || undefined),
    enabled: session === true,
    refetchInterval: 10_000,
  });
  const sites = useQuery({
    queryKey: ["sites"],
    queryFn: getSites,
    enabled: session === true,
    staleTime: 30_000,
  });
  const nodes = useQuery({
    queryKey: ["nodes"],
    queryFn: getNodes,
    enabled: session === true,
    staleTime: 30_000,
  });

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (alerts.isLoading || sites.isLoading || nodes.isLoading) return <LoadingState rows={7} />;
  if (alerts.isError) {
    return (
      <ErrorState
        error={alerts.error instanceof Error ? alerts.error.message : "Unable to load alerts."}
        onRetry={() => void alerts.refetch()}
      />
    );
  }

  const entries = alerts.data || [];
  const siteNames = new Map((sites.data || []).map((site) => [site.id, site.name]));
  const nodeNames = new Map((nodes.data || []).map((node) => [node.agent_id, node.name]));

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="OBSERVE"
        title="Alerts"
        description="Recent active and resolved conditions from the control plane."
        actions={
          <IconButton label="Refresh" onClick={() => void alerts.refetch()}>
            <RefreshCw size={16} />
          </IconButton>
        }
      />
      <Panel>
        <div className="table-toolbar">
          <div className="segmented-control" role="group" aria-label={t("Alerts")}>
            {([
              ["open", "Open alerts"],
              ["resolved", "Resolved conditions"],
              ["", "All conditions"],
            ] as const).map(([value, label]) => (
              <button
                className={status === value ? "segmented-active" : ""}
                key={value || "all"}
                onClick={() => setStatus(value)}
              >
                {t(label)}
              </button>
            ))}
          </div>
          <span className="toolbar-note">{t("{count} events", { count: formatNumber(entries.length) })}</span>
        </div>
        {entries.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("Alert")}</th>
                  <th>{t("Severity")}</th>
                  <th>{t("State")}</th>
                  <th>{t("Site")}</th>
                  <th>{t("Node")}</th>
                  <th>{t("First seen")}</th>
                  <th>{t("Last seen")}</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((alert) => (
                  <AlertRow
                    alert={alert}
                    formatDate={formatDate}
                    nodeName={nodeNames.get(alert.node_id) || alert.node_id}
                    siteName={siteNames.get(alert.site_id) || alert.site_id}
                    t={t}
                    key={alert.id}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No alerts recorded." />
        )}
      </Panel>
    </div>
  );
}

function AlertRow({
  alert,
  formatDate,
  nodeName,
  siteName,
  t,
}: {
  alert: Alert;
  formatDate: (value: string | null | undefined, withTime?: boolean) => string;
  nodeName: string;
  siteName: string;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  return (
    <tr>
      <td>
        <strong>{t(alert.title)}</strong>
        <span className="cell-secondary">{t(alert.detail) || "-"}</span>
      </td>
      <td><StatusBadge status={alert.severity} /></td>
      <td><StatusBadge status={alert.status} /></td>
      <td>{siteName ? t(siteName) : "-"}</td>
      <td>{nodeName || "-"}</td>
      <td>{formatDate(alert.first_seen_at)}</td>
      <td>{formatDate(alert.last_seen_at)}</td>
    </tr>
  );
}
