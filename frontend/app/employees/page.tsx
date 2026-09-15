"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Shield, UsersRound } from "lucide-react";
import { getEmployees, updateUserRole, type Employee } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { EmptyState, ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, Panel, StatusBadge } from "../../components/ui";

function roleLabel(role: Employee["role"], t: (key: string) => string) {
  if (role === "root") return t("Root administrator");
  if (role === "admin") return t("Administrator");
  return t("Employee");
}

export default function EmployeesPage() {
  const { formatDate, formatNumber, t } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const employees = useQuery({
    queryKey: ["employees"],
    queryFn: getEmployees,
    enabled: session === true,
    refetchInterval: 15_000,
  });
  const updateRole = useMutation({
    mutationFn: ({ itcode, role }: { itcode: string; role: "admin" | "employee" }) => updateUserRole(itcode, role),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["employees"] });
    },
  });

  if (session === null) return <LoadingState rows={7} />;
  if (!session) return <SessionGate />;
  if (employees.isLoading) return <LoadingState rows={7} />;
  if (employees.isError) return <ErrorState error={employees.error instanceof Error ? employees.error.message : t("Unable to load employees.")} onRetry={() => void employees.refetch()} />;

  const employeeItems = employees.data || [];
  return (
    <div className="page-stack page-fill list-page">
      <PageHeader
        eyebrow="GOVERN"
        title={t("Roles")}
        description={t("zhangle is the root administrator. Other accounts can be granted the same administrator privileges.")}
      />
      <Panel className="list-panel">
        <div className="table-toolbar">
          <div className="toolbar-title"><UsersRound size={18} /><span>{t("{count} accounts", { count: formatNumber(employeeItems.length) })}</span></div>
          <span className="toolbar-note">{t("Administrators and the root administrator have the same management permissions.")}</span>
        </div>
        {employeeItems.length ? (
          <div className="table-wrap table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{t("Account")}</th>
                  <th>{t("Role")}</th>
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
                    <td><StatusBadge status={employee.role === "employee" ? "pending" : "enabled"} /> {roleLabel(employee.role, t)}</td>
                    <td>{t(employee.auth_source)}</td>
                    <td><StatusBadge status={employee.is_active ? "active" : "disabled"} /></td>
                    <td>{formatDate(employee.last_login_at)}</td>
                    <td>{formatDate(employee.password_changed_at)}</td>
                    <td>{formatDate(employee.created_at)}</td>
                    <td>
                      {employee.role === "root" ? (
                        <span className="toolbar-note"><Shield size={14} /> {t("Immutable")}</span>
                      ) : employee.role === "admin" ? (
                        <Button
                          type="button"
                          disabled={updateRole.isPending}
                          onClick={() => updateRole.mutate({ itcode: employee.itcode, role: "employee" })}
                        >
                          {t("Revoke administrator")}
                        </Button>
                      ) : (
                        <Button
                          type="button"
                          variant="primary"
                          disabled={updateRole.isPending}
                          onClick={() => updateRole.mutate({ itcode: employee.itcode, role: "admin" })}
                        >
                          {t("Grant administrator")}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <EmptyState title={t("No accounts")} detail={t("Registered employees appear here after completing GQuan verification.")} />}
      </Panel>
    </div>
  );
}
