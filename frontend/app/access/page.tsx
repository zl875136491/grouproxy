"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Clipboard, Download, FileCode2, KeyRound, Laptop, ShieldCheck } from "lucide-react";
import { useState } from "react";
import {
  getAccessConfig,
  getEmployeeProxyAccess,
  getLinuxSetupScript,
  getProxyPAC,
  rotateOwnProxyCredential,
  type ProxyCredentialReveal,
} from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useAuthenticatedSession } from "../../components/session-gate";
import { Button, DetailDialog, Panel, StatusBadge } from "../../components/ui";

export default function AccessPage() {
  const { t, formatNumber } = usePreferences();
  const session = useAuthenticatedSession();
  const queryClient = useQueryClient();
  const [scriptCopied, setScriptCopied] = useState(false);
  const [credentialCopied, setCredentialCopied] = useState(false);
  const [revealed, setRevealed] = useState<ProxyCredentialReveal | null>(null);
  const script = useQuery({
    queryKey: ["linux-setup-script"],
    queryFn: getLinuxSetupScript,
    enabled: session === true,
    staleTime: 60_000,
  });
  const pac = useQuery({
    queryKey: ["proxy-pac"],
    queryFn: getProxyPAC,
    enabled: session === true,
    staleTime: 60_000,
  });
  const accessConfig = useQuery({
    queryKey: ["access-config"],
    queryFn: getAccessConfig,
    enabled: session === true,
    staleTime: 60_000,
  });
  const employeeAccess = useQuery({
    queryKey: ["employee-proxy-access"],
    queryFn: getEmployeeProxyAccess,
    enabled: session === true,
    staleTime: 30_000,
  });
  const rotate = useMutation({
    mutationFn: rotateOwnProxyCredential,
    onSuccess: async (credential) => {
      setRevealed(credential);
      await queryClient.invalidateQueries({ queryKey: ["employee-proxy-access"] });
    },
  });

  async function copyScript() {
    if (!script.data) return;
    await navigator.clipboard.writeText(script.data);
    setScriptCopied(true);
    window.setTimeout(() => setScriptCopied(false), 2_000);
  }

  async function copyCredential() {
    if (!revealed) return;
    await navigator.clipboard.writeText(revealed.password);
    setCredentialCopied(true);
    window.setTimeout(() => setCredentialCopied(false), 2_000);
  }

  function closeCredentialDialog(open: boolean) {
    if (open) return;
    setRevealed(null);
    setCredentialCopied(false);
    rotate.reset();
  }

  function downloadScript() {
    if (!script.data) return;
    const url = URL.createObjectURL(new Blob([script.data], { type: "text/x-shellscript" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "grouproxy-linux-setup.sh";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  if (session === null) return <LoadingState rows={6} />;
  if (!session) return <SessionGate />;
  if (script.isLoading || pac.isLoading || accessConfig.isLoading || employeeAccess.isLoading) {
    return <LoadingState rows={6} />;
  }
  const accessError = script.error || pac.error || accessConfig.error || employeeAccess.error;
  if (script.isError || pac.isError || accessConfig.isError || employeeAccess.isError) {
    return (
      <ErrorState
        error={accessError instanceof Error ? accessError.message : "Unable to load access configuration."}
        onRetry={() => void Promise.all([script.refetch(), pac.refetch(), accessConfig.refetch(), employeeAccess.refetch()])}
      />
    );
  }
  const config = accessConfig.data;
  const siteItems = employeeAccess.data?.sites || [];

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="GOVERN"
        title="Access"
        description="Employee proxy connection details, credentials, and the rendered Linux setup script."
      />
      <section className="access-grid">
        <Panel className="access-state-panel">
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">{t("CURRENT PATH")}</span>
              <h2>HTTP CONNECT</h2>
            </div>
            <StatusBadge status="enabled" />
          </div>
          <div className="access-facts">
            <div><span>FQDN</span><strong className="mono">{config?.fqdn || "-"}</strong></div>
            <div><span>{t("Proxy port")}</span><strong>{config ? formatNumber(config.port, { useGrouping: false }) : "-"}</strong></div>
            <div><span>{t("Transport")}</span><strong>HTTP CONNECT</strong></div>
          </div>
          <div className="access-note">
            <ShieldCheck size={18} />
            <span>{t("Destination HTTPS remains end-to-end inside the HTTP CONNECT tunnel.")}</span>
          </div>
        </Panel>
        <Panel>
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">LINUX</span>
              <h2>{t("Setup script")}</h2>
            </div>
            <Laptop size={19} />
          </div>
          <div className="script-actions">
            <Button variant="secondary" onClick={() => void copyScript()}><Clipboard size={16} /> {scriptCopied ? t("Copied") : t("Copy")}</Button>
            <Button variant="primary" onClick={downloadScript}><Download size={16} /> {t("Download")}</Button>
          </div>
          <pre className="script-view">{script.data}</pre>
        </Panel>
        <Panel className="credential-panel">
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">{t("IDENTITY")}</span>
              <h2>{t("Proxy credentials")}</h2>
            </div>
            <KeyRound size={19} />
          </div>
          {rotate.isError ? <div className="credential-error" role="alert">{t(rotate.error instanceof Error ? rotate.error.message : "request_failed")}</div> : null}
          <div className="credential-list">
            {siteItems.map((site) => (
              <div className="credential-row" key={site.id}>
                <div>
                  <strong>{t(site.name)}</strong>
                  <span>{site.proxy_auth_required ? t("Authentication required") : t("Network allowlist only")}</span>
                  {site.credential_configured && site.username ? <code>{site.username}</code> : null}
                </div>
                <div className="credential-row-actions">
                  <StatusBadge status={site.credential_configured ? "ready" : "pending"} />
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={rotate.isPending}
                    onClick={() => rotate.mutate(site.id)}
                  >
                    <KeyRound size={14} /> {site.credential_configured ? t("Rotate credential") : t("Create credential")}
                  </Button>
                </div>
              </div>
            ))}
          </div>
        </Panel>
        <Panel>
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">PAC</span>
              <h2>{t("Automatic proxy configuration")}</h2>
            </div>
            <FileCode2 size={19} />
          </div>
          <p className="panel-description">{t("PAC chooses the single HTTP listener and does not grant access.")}</p>
          <pre className="script-view">{pac.data}</pre>
        </Panel>
      </section>
      <DetailDialog
        open={Boolean(revealed)}
        onOpenChange={closeCredentialDialog}
        title="Proxy credential"
        contentClassName="credential-dialog-content"
      >
        {revealed ? (
          <div className="detail-stack">
            <dl className="detail-list">
              <div><dt>{t("Proxy username")}</dt><dd className="mono">{revealed.username}</dd></div>
              <div><dt>{t("One-time proxy password")}</dt><dd className="mono credential-secret">{revealed.password}</dd></div>
              {revealed.release_id ? <div><dt>{t("Credential release")}</dt><dd className="mono">{revealed.release_id}</dd></div> : null}
            </dl>
            <Button variant="primary" onClick={() => void copyCredential()}><Clipboard size={16} /> {credentialCopied ? t("Password copied") : t("Copy password")}</Button>
          </div>
        ) : null}
      </DetailDialog>
    </div>
  );
}
