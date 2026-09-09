"use client";

import { useQuery } from "@tanstack/react-query";
import {
  BookOpen,
  Check,
  Clipboard,
  Download,
  ExternalLink,
  FileCode2,
  Laptop,
  Link2,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { getAccessConfig, getLinuxSetupScript, getProxyPAC, getWindowsSetupScript } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { ErrorState, LoadingState } from "../../components/data-state";
import { notifyToast, toastErrorMessage } from "../../components/toast";
import { Button, StatusBadge } from "../../components/ui";

type CodeSnippetProps = {
  id: string;
  filename: string;
  language: string;
  content: string;
  copiedId: string;
  onCopy: () => void;
  onDownload?: () => void;
  t: (key: string, values?: Record<string, string | number>) => string;
};

function CodeSnippet({ id, filename, language, content, copiedId, onCopy, onDownload, t }: CodeSnippetProps) {
  const copied = copiedId === id;
  return (
    <div className="access-doc-codeblock">
      <div className="access-doc-codebar">
        <div className="access-doc-code-meta"><FileCode2 size={14} aria-hidden="true" /><code>{filename}</code><span>{language}</span></div>
        <div className="row-actions">
          <Button size="sm" onClick={onCopy}>{copied ? <Check size={14} /> : <Clipboard size={14} />}{copied ? t("Copied") : t("Copy")}</Button>
          {onDownload ? <Button size="sm" onClick={onDownload}><Download size={14} />{t("Download")}</Button> : null}
        </div>
      </div>
      <pre className="access-doc-code"><code>{content}</code></pre>
    </div>
  );
}

function SectionHeading({ id, eyebrow, title, description, children, t }: { id: string; eyebrow: string; title: string; description?: string; children?: ReactNode; t: (key: string, values?: Record<string, string | number>) => string }) {
  return (
    <div className="access-doc-section-heading">
      <div><span className="access-doc-eyebrow">{t(eyebrow)}</span><h2 id={id}>{t(title)}</h2>{description ? <p>{t(description)}</p> : null}</div>
      {children}
    </div>
  );
}

function downloadText(content: string, filename: string, mime = "text/plain;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function shellQuote(value: string) {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

async function writeClipboard(value: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("clipboard_unavailable");
}

export default function AccessPage() {
  const { t, formatNumber } = usePreferences();
  const [copiedId, setCopiedId] = useState("");
  const accessConfig = useQuery({ queryKey: ["access-config"], queryFn: getAccessConfig, staleTime: 60_000 });
  const linuxScript = useQuery({ queryKey: ["linux-setup-script"], queryFn: getLinuxSetupScript, staleTime: 60_000 });
  const windowsScript = useQuery({ queryKey: ["windows-setup-script"], queryFn: getWindowsSetupScript, staleTime: 60_000 });
  const pac = useQuery({ queryKey: ["proxy-pac"], queryFn: getProxyPAC, staleTime: 60_000 });

  const config = accessConfig.data;
  const endpoint = config ? `http://${config.fqdn}:${config.port}` : "http://proxy.example.com:1080";
  const shellEndpoint = shellQuote(endpoint);
  const quickCommand = `export http_proxy=${shellEndpoint} https_proxy=${shellEndpoint} HTTP_PROXY=${shellEndpoint} HTTPS_PROXY=${shellEndpoint}`;
  const verificationCommand = `curl --fail --silent --show-error --proxy ${shellEndpoint} https://ipinfo.io/json`;
  const enableCommand = "chmod +x ./grouproxy-linux-setup.sh && ./grouproxy-linux-setup.sh";
  const disableCommand = "./grouproxy-linux-setup.sh --uninstall";

  const copyContent = async (id: string, content: string, label: string) => {
    try {
      await writeClipboard(content);
      setCopiedId(id);
      window.setTimeout(() => setCopiedId((current) => current === id ? "" : current), 1_800);
      notifyToast({ title: t("Copied"), description: label, variant: "success" });
    } catch (error) {
      notifyToast({ title: t("Operation failed"), description: t(toastErrorMessage(error)), variant: "destructive" });
    }
  };

  const snippets = { quick: quickCommand, verify: verificationCommand, enable: enableCommand, disable: disableCommand };

  if (accessConfig.isLoading || linuxScript.isLoading || windowsScript.isLoading || pac.isLoading) return <LoadingState rows={7} />;
  const issue = accessConfig.error || linuxScript.error || windowsScript.error || pac.error;
  if (accessConfig.isError || linuxScript.isError || windowsScript.isError || pac.isError) {
    return <ErrorState error={issue instanceof Error ? issue.message : "Unable to load access configuration."} onRetry={() => void Promise.all([accessConfig.refetch(), linuxScript.refetch(), windowsScript.refetch(), pac.refetch()])} />;
  }
  if (!config || !linuxScript.data || !windowsScript.data || !pac.data) return <LoadingState rows={7} />;

  function copySnippet(id: string, content: string, label: string) {
    void copyContent(id, content, label);
  }

  return (
    <div className="page-fill access-docs-page">
      <div className="access-docs-layout">
        <main className="access-docs-article">
          <header className="access-docs-header">
            <div className="access-docs-title-row"><div className="access-docs-mark"><BookOpen size={18} /></div><div><span className="access-doc-eyebrow">{t("ACCESS GUIDE")}</span><h1>{t("Proxy access")}</h1></div></div>
            <p>{t("Configure the Grouproxy HTTP CONNECT endpoint on a workstation, verify the route, and return to a direct connection when finished.")}</p>
            <div className="access-endpoint-bar"><div className="access-endpoint-status"><StatusBadge status="enabled" /><span>{t(config.environment === "test" ? "Test environment" : "Production environment")}</span></div><code>{endpoint}</code><span className="access-endpoint-port">{t("Port")} {formatNumber(config.port, { useGrouping: false })}</span></div>
          </header>

          <section className="access-doc-section" aria-labelledby="access-overview">
            <SectionHeading id="access-overview" eyebrow="OVERVIEW" title="Proxy endpoint" description="The proxy service is exposed directly on port 1080. Dashboard access is separate and does not require an NGINX forwarding layer." t={t} />
            <div className="access-doc-callout"><Link2 size={17} /><div><strong>{t("HTTP CONNECT")}</strong><span>{t("Destination HTTPS remains end-to-end inside the HTTP CONNECT tunnel. No proxy credentials or TLS interception are used.")}</span></div></div>
            <dl className="access-doc-facts"><div><dt>{t("Environment")}</dt><dd>{t(config.environment === "test" ? "Test environment" : "Production environment")}</dd></div><div><dt>FQDN</dt><dd className="mono">{config.fqdn}</dd></div><div><dt>{t("Port")}</dt><dd>{formatNumber(config.port, { useGrouping: false })}</dd></div><div><dt>{t("Protocol")}</dt><dd>HTTP CONNECT</dd></div></dl>
          </section>

          <section className="access-doc-section" aria-labelledby="access-quick-start">
            <SectionHeading id="access-quick-start" eyebrow="QUICK START" title="Configure a shell in one line" description="The variables apply to this terminal and the processes started from it." t={t} />
            <CodeSnippet id="quick" filename="shell" language="bash" content={snippets.quick} copiedId={copiedId} onCopy={() => copySnippet("quick", snippets.quick, t("Configure a shell in one line"))} onDownload={() => downloadText(snippets.quick, "grouproxy-proxy-env.sh", "text/x-shellscript")} t={t} />
            <ol className="access-doc-steps"><li>{t("Paste the command into a new terminal.")}</li><li>{t("Run the verification command below before opening applications.")}</li><li>{t("Close the terminal or run the disable command to return to direct access.")}</li></ol>
          </section>

          <section className="access-doc-section" aria-labelledby="access-windows">
            <SectionHeading id="access-windows" eyebrow="WINDOWS" title="Windows one-click setup" description="The pre-generated PowerShell asset configures the current Windows user and keeps a backup for a reversible disable operation." t={t}><span className="access-doc-download-note"><Download size={14} />{t("Download ready")}</span></SectionHeading>
            <CodeSnippet id="windows" filename="grouproxy-windows-setup.ps1" language="powershell" content={windowsScript.data} copiedId={copiedId} onCopy={() => copySnippet("windows", windowsScript.data, t("Windows one-click setup"))} onDownload={() => downloadText(windowsScript.data, "grouproxy-windows-setup.ps1", "text/plain;charset=utf-8")} t={t} />
            <div className="access-doc-instructions"><p><strong>1.</strong> {t("Download the script, then open PowerShell in its download folder.")}</p><p><strong>2.</strong> <code>Set-ExecutionPolicy -Scope Process Bypass</code>, then run <code>.\grouproxy-windows-setup.ps1</code>.</p><p><strong>3.</strong> {t("Use -SkipDirectTest to skip the optional direct-network checks, or -Disable to restore the previous Windows proxy settings.")}</p></div>
          </section>

          <section className="access-doc-section" aria-labelledby="access-macos">
            <SectionHeading id="access-macos" eyebrow="MACOS" title="macOS shortcut" description="Use the shortcut prepared for the selected deployment environment." t={t} />
            <div className="access-doc-action-row"><a className="access-doc-link-button" href={config.macos_shortcut_url} target="_blank" rel="noreferrer"><Laptop size={17} />{t("Get shortcut")}<ExternalLink size={14} /></a><span>{t("Opens Apple Shortcuts in a new tab.")}</span></div>
            <ol className="access-doc-steps"><li>{t("Open the environment-specific iCloud link.")}</li><li>{t("Review the actions and add the shortcut to the Shortcuts app.")}</li><li>{t("Run it from Shortcuts and follow the prompts shown by macOS.")}</li></ol>
          </section>

          <section className="access-doc-section" aria-labelledby="access-linux">
            <SectionHeading id="access-linux" eyebrow="LINUX" title="Linux desktop and shell setup" description="The repository-provided script configures shell variables and GNOME or KDE settings when available." t={t} />
            <CodeSnippet id="linux" filename="grouproxy-linux-setup.sh" language="bash" content={linuxScript.data} copiedId={copiedId} onCopy={() => copySnippet("linux", linuxScript.data, t("Linux desktop and shell setup"))} onDownload={() => downloadText(linuxScript.data, "grouproxy-linux-setup.sh", "text/x-shellscript")} t={t} />
            <div className="access-doc-instructions"><p><strong>1.</strong> {t("Download the script and make it executable.")}</p><p><strong>2.</strong> {t("Run it as the current user for shell and desktop settings.")}</p><p><strong>3.</strong> {t("Use --no-desktop for shell-only setup, --system for system defaults as root, or --uninstall to remove only Grouproxy-managed settings.")}</p></div>
          </section>

          <section className="access-doc-section" aria-labelledby="access-verify">
            <SectionHeading id="access-verify" eyebrow="VERIFY" title="Test the actual proxy route" description="These commands make a real HTTPS request through HTTP CONNECT and show the proxy exit address." t={t} />
            <CodeSnippet id="verify" filename="verify-proxy.sh" language="bash" content={snippets.verify} copiedId={copiedId} onCopy={() => copySnippet("verify", snippets.verify, t("Test the actual proxy route"))} onDownload={() => downloadText(snippets.verify, "grouproxy-verify-proxy.sh", "text/x-shellscript")} t={t} />
            <div className="access-doc-action-row"><button className="access-doc-link-button access-doc-link-secondary" type="button" onClick={() => downloadText(pac.data, "grouproxy-proxy.pac", "application/x-ns-proxy-autoconfig")}><Download size={16} />{t("Download PAC file")}</button><span>{t("PAC chooses the single HTTP listener and does not grant access.")}</span></div>
          </section>

          <section className="access-doc-section" aria-labelledby="access-notes">
            <SectionHeading id="access-notes" eyebrow="NOTES" title="Connection behavior" description="Keep these details in mind when troubleshooting workstation access." t={t} />
            <ul className="access-doc-notes"><li>{t("The proxy is HTTP CONNECT on TCP 1080; HTTPS destinations remain encrypted end-to-end.")}</li><li>{t("The endpoint has no proxy authentication layer. Network policy and site CIDRs control access.")}</li><li>{t("Existing applications may need to be restarted after enabling or disabling environment variables.")}</li></ul>
            <div className="access-doc-command-pair"><CodeSnippet id="enable" filename={t("Linux enable")} language="bash" content={snippets.enable} copiedId={copiedId} onCopy={() => copySnippet("enable", snippets.enable, t("Linux enable"))} t={t} /><CodeSnippet id="disable" filename={t("Linux disable")} language="bash" content={snippets.disable} copiedId={copiedId} onCopy={() => copySnippet("disable", snippets.disable, t("Linux disable"))} t={t} /></div>
          </section>
        </main>

        <aside className="access-docs-rail" aria-label={t("On this page")}><div className="access-docs-rail-sticky"><div className="access-docs-rail-title">{t("On this page")}</div>{[["access-overview", "Overview"], ["access-quick-start", "Quick start"], ["access-windows", "Windows"], ["access-macos", "macOS"], ["access-linux", "Linux"], ["access-verify", "Verification"], ["access-notes", "Notes"]].map(([id, label]) => <a href={`#${id}`} key={id}>{t(label)}</a>)}<div className="access-docs-rail-endpoint"><span>{t("Environment")}</span><strong>{t(config.environment === "test" ? "Test environment" : "Production environment")}</strong><code>{config.fqdn}:{config.port}</code></div></div></aside>
      </div>
    </div>
  );
}
