"use client";

import { useQuery } from "@tanstack/react-query";
import { Clipboard, Download, Laptop, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { getLinuxSetupScript } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, Panel, StatusBadge } from "../../components/ui";

export default function AccessPage() {
  const { t } = usePreferences();
  const session = useManagementSession();
  const [copied, setCopied] = useState(false);
  const script = useQuery({ queryKey: ["linux-setup-script"], queryFn: getLinuxSetupScript, enabled: session === true, staleTime: 60_000 });

  async function copyScript() {
    if (!script.data) return;
    await navigator.clipboard.writeText(script.data);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2_000);
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
  if (script.isLoading) return <LoadingState rows={6} />;
  if (script.isError) return <ErrorState error={script.error instanceof Error ? script.error.message : "Unable to render the Linux setup script."} onRetry={() => void script.refetch()} />;

  return (
    <div className="page-stack">
      <PageHeader eyebrow="GOVERN" title="Access" description="Employee proxy connection details and the rendered Linux setup script." />
      <section className="access-grid">
        <Panel className="access-state-panel"><div className="panel-heading"><div><span className="panel-kicker">{t("CURRENT PATH")}</span><h2>HTTP CONNECT</h2></div><StatusBadge status="enabled" /></div><div className="access-facts"><div><span>FQDN</span><strong className="mono">proxy.corp.internal</strong></div><div><span>{t("Proxy port")}</span><strong>80</strong></div><div><span>{t("HTTPS proxy listener")}</span><strong>{t("Disabled")}</strong></div></div><div className="access-note"><ShieldCheck size={18} /><span>{t("Destination HTTPS remains end-to-end inside the HTTP CONNECT tunnel.")}</span></div></Panel>
        <Panel><div className="panel-heading"><div><span className="panel-kicker">LINUX</span><h2>{t("Setup script")}</h2></div><Laptop size={19} /></div><div className="script-actions"><Button variant="secondary" onClick={() => void copyScript()}><Clipboard size={16} /> {copied ? t("Copied") : t("Copy")}</Button><Button variant="primary" onClick={downloadScript}><Download size={16} /> {t("Download")}</Button></div><pre className="script-view">{script.data}</pre></Panel>
      </section>
    </div>
  );
}
