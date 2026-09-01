"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Clipboard, KeyRound, UsersRound } from "lucide-react";
import { useState } from "react";
import {
  getEmployeeProxyCredentials,
  getEmployees,
  getSites,
  rotateEmployeeProxyCredential,
  type Employee,
  type ProxyCredentialReveal,
} from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, DetailDialog, Panel, StatusBadge } from "../../components/ui";

export default function EmployeesPage() {
  const { formatDate, formatNumber, t } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Employee | null>(null);
  const [revealed, setRevealed] = useState<ProxyCredentialReveal | null>(null);
  const [copied, setCopied] = useState(false);
  const employees = useQuery({
    queryKey: ["employees"],
    queryFn: getEmployees,
    enabled: session === true,
    refetchInterval: 15_000,
  });
  const sites = useQuery({
    queryKey: ["sites"],
    queryFn: getSites,
    enabled: session === true,
    staleTime: 30_000,
  });
  const credentials = useQuery({
    queryKey: ["employee-proxy-credentials", selected?.itcode],
    queryFn: () => getEmployeeProxyCredentials(selected!.itcode),
    enabled: session === true && Boolean(selected),
  });
  const rotate = useMutation({
    mutationFn: ({ siteId, itcode }: { siteId: string; itcode: string }) =>
      rotateEmployeeProxyCredential(siteId, itcode),
    onSuccess: async (credential) => {
      setSelected(null);
      setRevealed(credential);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["employee-proxy-credentials"] }),
        queryClient.invalidateQueries({ queryKey: ["releases"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      ]);
    },
  });

  async function copyPassword() {
    if (!revealed) return;
    await navigator.clipboard.writeText(revealed.password);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2_000);
  }

  function closeReveal(open: boolean) {
    if (open) return;
    setRevealed(null);
    setCopied(false);
    rotate.reset();
  }

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (employees.isLoading || sites.isLoading) return <LoadingState rows={7} />;
  if (employees.isError || sites.isError) {
    const error = employees.error || sites.error;
    return <ErrorState error={error instanceof Error ? error.message : "Unable to load employees."} onRetry={() => void Promise.all([employees.refetch(), sites.refetch()])} />;
  }

  const employeeItems = employees.data || [];
  const siteItems = sites.data || [];
  const credentialsBySite = new Map((credentials.data || []).map((credential) => [credential.site_id, credential]));

  return (
    <div className="page-stack">
      <PageHeader eyebrow="GOVERN" title="Employees" description="Local employee accounts and HTTP Basic credentials for sites that require authentication." />
      <Panel>
        <div className="table-toolbar">
          <div className="toolbar-title"><UsersRound size={18} /><span>{t("{count} employees", { count: formatNumber(employeeItems.length) })}</span></div>
          <span className="toolbar-note">{t("Registered employees appear here after completing GQuan verification.")}</span>
        </div>
        {employeeItems.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("Employee")}</th>
                  <th>{t("Authentication source")}</th>
                  <th>{t("State")}</th>
                  <th>{t("Last sign-in")}</th>
                  <th>{t("Password changed")}</th>
                  <th>{t("Created")}</th>
                  <th aria-label={t("Actions")} />
                </tr>
              </thead>
              <tbody>
                {employeeItems.map((employee) => (
                  <tr key={employee.itcode}>
                    <td className="mono">{employee.itcode}</td>
                    <td>{t(employee.auth_source)}</td>
                    <td><StatusBadge status={employee.is_active ? "active" : "disabled"} /></td>
                    <td>{formatDate(employee.last_login_at)}</td>
                    <td>{formatDate(employee.password_changed_at)}</td>
                    <td>{formatDate(employee.created_at)}</td>
                    <td><Button size="sm" variant="ghost" onClick={() => { rotate.reset(); setSelected(employee); }}><KeyRound size={14} /> {t("Manage")}</Button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <EmptyState title="No employee accounts" detail="Registered employees appear here after completing GQuan verification." />}
      </Panel>

      <DetailDialog
        open={Boolean(selected)}
        onOpenChange={(open) => { if (!open && !rotate.isPending) setSelected(null); }}
        title="Employee proxy credentials"
        description={selected?.itcode}
        contentClassName="credential-dialog-content"
      >
        {selected ? (
          <div className="detail-stack">
            <p className="panel-description">{t("Credentials are revealed once and delivered through a release when a site requires authentication.")}</p>
            {rotate.isError ? <div className="credential-error" role="alert">{t(rotate.error instanceof Error ? rotate.error.message : "The credential could not be issued.")}</div> : null}
            {credentials.isLoading ? <LoadingState rows={3} /> : credentials.isError ? <ErrorState error="Unable to load employee credentials." onRetry={() => void credentials.refetch()} /> : (
              <div className="credential-list">
                {siteItems.map((site) => {
                  const credential = credentialsBySite.get(site.id);
                  const configured = Boolean(credential?.active);
                  return (
                    <div className="credential-row" key={site.id}>
                      <div>
                        <strong>{t(site.name)}</strong>
                        <span>{site.proxy_auth_required ? t("Authentication required") : t("Network allowlist only")}</span>
                        {credential ? <code>{credential.username}</code> : null}
                      </div>
                      <div className="credential-row-actions">
                        <StatusBadge status={configured ? "ready" : "pending"} />
                        <Button size="sm" variant="ghost" disabled={rotate.isPending} onClick={() => rotate.mutate({ siteId: site.id, itcode: selected.itcode })}>
                          <KeyRound size={14} /> {t(configured ? "Rotate credential" : "Issue credential")}
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        ) : null}
      </DetailDialog>

      <DetailDialog open={Boolean(revealed)} onOpenChange={closeReveal} title="Proxy credential" contentClassName="credential-dialog-content">
        {revealed ? (
          <div className="detail-stack">
            <dl className="detail-list">
              <div><dt>{t("Proxy username")}</dt><dd className="mono">{revealed.username}</dd></div>
              <div><dt>{t("One-time proxy password")}</dt><dd className="mono credential-secret">{revealed.password}</dd></div>
              {revealed.release_id ? <div><dt>{t("Credential release")}</dt><dd className="mono">{revealed.release_id}</dd></div> : <div><dt>{t("Credential release")}</dt><dd>{t("Credential is ready for the next policy release.")}</dd></div>}
            </dl>
            <Button variant="primary" onClick={() => void copyPassword()}><Clipboard size={16} /> {copied ? t("Password copied") : t("Copy password")}</Button>
          </div>
        ) : null}
      </DetailDialog>
    </div>
  );
}
