"use client";

import { useQuery } from "@tanstack/react-query";
import { UsersRound } from "lucide-react";
import { getEmployees } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Panel, StatusBadge } from "../../components/ui";

export default function EmployeesPage() {
  const { formatDate, formatNumber, t } = usePreferences();
  const session = useManagementSession();
  const employees = useQuery({
    queryKey: ["employees"],
    queryFn: getEmployees,
    enabled: session === true,
    refetchInterval: 15_000,
  });

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (employees.isLoading) return <LoadingState rows={7} />;
  if (employees.isError) return <ErrorState error={employees.error instanceof Error ? employees.error.message : "Unable to load employees."} onRetry={() => void employees.refetch()} />;

  const employeeItems = employees.data || [];
  return (
    <div className="page-stack page-fill list-page">
      <PageHeader eyebrow="GOVERN" title="Employees" description="Local employee account inventory." />
      <Panel className="list-panel">
        <div className="table-toolbar"><div className="toolbar-title"><UsersRound size={18} /><span>{t("{count} employees", { count: formatNumber(employeeItems.length) })}</span></div><span className="toolbar-note">{t("Registered employees appear here after completing GQuan verification.")}</span></div>
        {employeeItems.length ? <div className="table-wrap table-scroll"><table><thead><tr><th>{t("Employee")}</th><th>{t("Authentication source")}</th><th>{t("State")}</th><th>{t("Last sign-in")}</th><th>{t("Password changed")}</th><th>{t("Created")}</th></tr></thead><tbody>{employeeItems.map((employee) => <tr key={employee.itcode}><td className="mono">{employee.itcode}</td><td>{t(employee.auth_source)}</td><td><StatusBadge status={employee.is_active ? "active" : "disabled"} /></td><td>{formatDate(employee.last_login_at)}</td><td>{formatDate(employee.password_changed_at)}</td><td>{formatDate(employee.created_at)}</td></tr>)}</tbody></table></div> : <EmptyState title="No employee accounts" detail="Registered employees appear here after completing GQuan verification." />}
      </Panel>
    </div>
  );
}
