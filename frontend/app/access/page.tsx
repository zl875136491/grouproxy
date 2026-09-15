"use client";

import { useQuery } from "@tanstack/react-query";
import {
  BookOpen,
  Check,
  Clipboard,
  Clock3,
  Download,
  FileCode2,
  Laptop,
  Monitor,
  Terminal,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { getAccessConfig, getLinuxSetupScript, getWindowsSetupScript } from "../../lib/api";
import { writeClipboard } from "../../lib/clipboard";
import { usePreferences } from "../../lib/preferences";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { notifyToast, toastErrorMessage } from "../../components/toast";
import { IconButton, StatusBadge } from "../../components/ui";

type CodeLanguage = "bash" | "powershell";

type CodeSnippetProps = {
  id: string;
  filename: string;
  language: CodeLanguage;
  content: string;
  copiedId: string;
  onCopy: () => void;
};

const bashTokens = /(#.*$)|(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')|(\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)|(\b(?:export|if|then|fi|for|in|do|done|case|esac|function|local)\b)|(--?[A-Za-z][A-Za-z0-9-]*)|(https?:\/\/[^\s\"'`]+)/gm;
const powerShellTokens = /(#.*$)|(\"(?:`.|[^\"`])*\"|'(?:''|[^'])*')|(\$[A-Za-z_][A-Za-z0-9_:]*)|(\b(?:param|if|else|elseif|foreach|function|return|exit|switch)\b)|(-{1,2}[A-Za-z][A-Za-z0-9-]*)|(\[[A-Za-z][A-Za-z0-9.]*\])|(https?:\/\/[^\s\"'`]+)/gim;

function tokenClass(match: RegExpMatchArray, language: CodeLanguage) {
  if (match[1]) return "access-doc-token-comment";
  if (match[2]) return "access-doc-token-string";
  if (match[3]) return "access-doc-token-variable";
  if (match[4]) return "access-doc-token-keyword";
  if (match[5]) return "access-doc-token-option";
  if (language === "powershell" && match[6]) return "access-doc-token-type";
  return "access-doc-token-url";
}

function highlightCode(content: string, language: CodeLanguage) {
  const pattern = language === "powershell" ? powerShellTokens : bashTokens;
  const fragments: ReactNode[] = [];
  let cursor = 0;
  let tokenIndex = 0;

  for (const match of content.matchAll(pattern)) {
    const start = match.index ?? cursor;
    if (start > cursor) fragments.push(content.slice(cursor, start));
    fragments.push(<span className={tokenClass(match, language)} key={`${start}-${tokenIndex}`}>{match[0]}</span>);
    cursor = start + match[0].length;
    tokenIndex += 1;
  }

  if (cursor < content.length) fragments.push(content.slice(cursor));
  return fragments;
}

function CodeSnippet({ id, filename, language, content, copiedId, onCopy }: CodeSnippetProps) {
  const copied = copiedId === id;
  return (
    <div className="access-doc-codeblock">
      <div className="access-doc-codebar">
        <div className="access-doc-code-meta"><FileCode2 size={14} aria-hidden="true" /><code>{filename}</code><span>{language}</span></div>
        <div className="row-actions">
          <IconButton className="access-doc-code-action" label={copied ? "Copied" : "Copy"} onClick={onCopy}>{copied ? <Check size={14} /> : <Clipboard size={14} />}</IconButton>
        </div>
      </div>
      <pre className="access-doc-code"><code>{highlightCode(content, language)}</code></pre>
    </div>
  );
}

function SectionHeading({ id, title, description }: { id: string; title: string; description: string }) {
  return <div className="access-doc-section-heading"><div><h2 id={id}>{title}</h2><p>{description}</p></div></div>;
}

function PlatformCard({ id, icon, title, description, children }: { id: string; icon: ReactNode; title: string; description: string; children: ReactNode }) {
  return (
    <article className="access-doc-platform-card" aria-labelledby={id}>
      <header>
        <span className="access-doc-platform-icon" aria-hidden="true">{icon}</span>
        <div><h3 id={id}>{title}</h3><p>{description}</p></div>
      </header>
      <div className="access-doc-card-body">{children}</div>
    </article>
  );
}

function MacOSShortcutLink({ href, filename, title, unavailable = false, t }: { href?: string; filename?: string; title: string; unavailable?: boolean; t: (key: string, values?: Record<string, string | number>) => string }) {
  const content = <><img src="/apple-shortcuts-icon.jpg" alt="" width={44} height={44} /><span className="access-doc-shortcut-copy"><strong>{title}</strong><span>{t("macOS shortcut")}</span></span>{unavailable ? <Clock3 size={16} aria-hidden="true" /> : <Download size={16} aria-hidden="true" />}</>;
  if (unavailable) {
    return (
      <button className="access-doc-shortcut-link" type="button" onClick={() => notifyToast({ title: t("Feature in development") })} aria-label={`${title} - ${t("Feature in development")}`}>
        {content}
      </button>
    );
  }
  return (
    <a className="access-doc-shortcut-link" href={href} download={filename} aria-label={`${title} - ${t("Download")}`}>
      {content}
    </a>
  );
}

function ScriptDownloadLink({ icon, title, subtitle, onDownload, t }: { icon: ReactNode; title: string; subtitle: string; onDownload: () => void; t: (key: string, values?: Record<string, string | number>) => string }) {
  return (
    <button className="access-doc-shortcut-link access-doc-script-download-link" type="button" onClick={onDownload} aria-label={`${title} - ${t("Download")}`}>
      <span className="access-doc-download-icon" aria-hidden="true">{icon}</span>
      <span className="access-doc-shortcut-copy"><strong>{title}</strong><span>{subtitle}</span></span>
      <Download size={16} aria-hidden="true" />
    </button>
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

const macOSShortcutDownloads = {
  test: {
    href: "/shortcuts/grouproxy-macos-test.shortcut",
    filename: "grouproxy-macos-test.shortcut",
  },
  production: {
    href: "/shortcuts/grouproxy-macos-production.shortcut",
    filename: "grouproxy-macos-production.shortcut",
  },
} as const;

export default function AccessPage() {
  const { t, formatNumber } = usePreferences();
  const [copiedId, setCopiedId] = useState("");
  const accessConfig = useQuery({ queryKey: ["access-config"], queryFn: getAccessConfig, staleTime: 60_000 });
  const linuxScript = useQuery({ queryKey: ["linux-setup-script"], queryFn: getLinuxSetupScript, staleTime: 60_000 });
  const windowsScript = useQuery({ queryKey: ["windows-setup-script"], queryFn: getWindowsSetupScript, staleTime: 60_000 });

  const config = accessConfig.data;
  const endpoint = config ? `http://${config.fqdn}:${config.port}` : "http://proxy.example.com:1080";
  const linuxQuickCommand = config ? `export HTTP_PROXY=${shellQuote(endpoint)}\nexport HTTPS_PROXY="$HTTP_PROXY"\nexport NO_PROXY="localhost,127.0.0.1,::1"` : "";
  const windowsQuickCommand = config ? `$settings = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings"\nSet-ItemProperty -Path $settings -Name ProxyServer -Value "${config.fqdn}:${config.port}"\nSet-ItemProperty -Path $settings -Name ProxyEnable -Value 1` : "";
  const windowsValidationCommand = config ? `$ProxyUrl = \"${endpoint}\"\nTest-NetConnection -ComputerName \"${config.fqdn}\" -Port ${config.port}\nInvoke-RestMethod -Uri \"https://ipinfo.io/ip\" -Proxy $ProxyUrl` : "";
  const linuxValidationCommand = config ? `proxy=${shellQuote(endpoint)}\ncurl --connect-timeout 5 --fail --silent --show-error --proxy \"$proxy\" https://ipinfo.io/ip` : "";
  const macOSShortcut = config ? macOSShortcutDownloads[config.environment] : undefined;
  const accessReady = Boolean(config && linuxScript.data && windowsScript.data);

  useEffect(() => {
    if (!accessReady) return;
    const anchorId = decodeURIComponent(window.location.hash.slice(1));
    if (!anchorId) return;
    const frame = window.requestAnimationFrame(() => document.getElementById(anchorId)?.scrollIntoView());
    return () => window.cancelAnimationFrame(frame);
  }, [accessReady]);

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

  if (accessConfig.isLoading || linuxScript.isLoading || windowsScript.isLoading) return <LoadingState rows={7} />;
  const issue = accessConfig.error || linuxScript.error || windowsScript.error;
  if (accessConfig.isError || linuxScript.isError || windowsScript.isError) {
    return <ErrorState error={issue instanceof Error ? issue.message : "Unable to load access configuration."} onRetry={() => void Promise.all([accessConfig.refetch(), linuxScript.refetch(), windowsScript.refetch()])} />;
  }
  if (!config || !linuxScript.data || !windowsScript.data) return <LoadingState rows={7} />;

  function copySnippet(id: string, content: string, label: string) {
    void copyContent(id, content, label);
  }

  return (
    <div className="page-fill access-docs-page">
      <div className="access-docs-layout">
        <main className="access-docs-article">
          <section className="access-docs-header">
            <PageHeader
              className="access-docs-page-header"
              eyebrow="ACCESS GUIDE"
              title="Proxy access"
              description="Connect your workstation, verify the route, and use reusable setup scripts from one place."
              icon={<BookOpen size={18} />}
            />
            <div className="access-endpoint-bar"><div className="access-endpoint-status"><StatusBadge status="enabled" /><span>{t(config.environment === "test" ? "Test environment" : "Production environment")}</span></div><code>{endpoint}</code><span className="access-endpoint-port">{t("Port")} {formatNumber(config.port, { useGrouping: false })}</span></div>
          </section>

          <section className="access-doc-section" aria-labelledby="access-quick-start">
            <SectionHeading id="access-quick-start" title={t("Quick start")} description={t("Choose an operating system and use the shortest supported setup path.")} />
            <div className="access-doc-platform-grid">
              <PlatformCard id="access-quick-linux" icon={<Terminal size={19} />} title={t("Linux")} description={t("Set proxy variables for the current shell.")}>
                <CodeSnippet id="linux-quick" filename="proxy-env.sh" language="bash" content={linuxQuickCommand} copiedId={copiedId} onCopy={() => copySnippet("linux-quick", linuxQuickCommand, t("Linux"))} />
                <p className="access-doc-card-note">{t("These variables apply to the current shell and its child processes.")}</p>
              </PlatformCard>
              <PlatformCard id="access-quick-windows" icon={<Monitor size={19} />} title={t("Windows")} description={t("Set the current user's Windows system proxy directly.")}>
                <CodeSnippet id="windows-quick" filename="set-proxy.ps1" language="powershell" content={windowsQuickCommand} copiedId={copiedId} onCopy={() => copySnippet("windows-quick", windowsQuickCommand, t("Windows"))} />
                <p className="access-doc-card-note">{t("Set ProxyEnable to 0 in the same location to turn the proxy off.")}</p>
              </PlatformCard>
              <PlatformCard id="access-quick-macos" icon={<Laptop size={19} />} title={t("macOS")} description={t("Download the shortcut, import it into the Shortcuts app, and run it.")}>
                <MacOSShortcutLink href={macOSShortcut?.href} filename={macOSShortcut?.filename} title={t("Enable proxy script")} t={t} />
              </PlatformCard>
            </div>
          </section>

          <section className="access-doc-section" aria-labelledby="access-testing">
            <SectionHeading id="access-testing" title={t("Testing and validation")} description={t("Confirm the listener and the actual HTTP CONNECT route.")} />
            <div className="access-doc-platform-grid">
              <PlatformCard id="access-testing-linux" icon={<Terminal size={19} />} title={t("Linux")} description={t("Check the listener and verify traffic reaches the network through the proxy.")}>
                <CodeSnippet id="linux-validation" filename="grouproxy-check.sh" language="bash" content={linuxValidationCommand} copiedId={copiedId} onCopy={() => copySnippet("linux-validation", linuxValidationCommand, t("Linux"))} />
              </PlatformCard>
              <PlatformCard id="access-testing-windows" icon={<Monitor size={19} />} title={t("Windows")} description={t("Check the listener and verify traffic reaches the network through the proxy.")}>
                <CodeSnippet id="windows-validation" filename="grouproxy-check.ps1" language="powershell" content={windowsValidationCommand} copiedId={copiedId} onCopy={() => copySnippet("windows-validation", windowsValidationCommand, t("Windows"))} />
              </PlatformCard>
              <PlatformCard id="access-testing-macos" icon={<Laptop size={19} />} title={t("macOS")} description={t("This macOS shortcut is in development.")}>
                <MacOSShortcutLink unavailable title={t("Test the actual proxy route")} t={t} />
              </PlatformCard>
            </div>
          </section>

          <section className="access-doc-section" aria-labelledby="access-reusable-scripts">
            <SectionHeading id="access-reusable-scripts" title={t("Reusable scripts")} description={t("Download the script once, then run the same file whenever you need to switch the proxy on or off.")} />
            <div className="access-doc-platform-grid">
              <PlatformCard id="access-reusable-linux" icon={<Terminal size={19} />} title={t("Linux")} description={t("Toggle proxy settings for the current user without command-line parameters.")}>
                <ScriptDownloadLink icon={<Terminal size={24} />} title={t("grouproxy-linux-setup.sh")} subtitle={t("Bash script")} onDownload={() => downloadText(linuxScript.data, "grouproxy-linux-setup.sh", "text/x-shellscript")} t={t} />
                <p className="access-doc-card-note">{t("After downloading, make the file executable and run it. Each run checks the current proxy state and switches it to the opposite state; then reopen the terminal and affected applications.")}</p>
              </PlatformCard>
              <PlatformCard id="access-reusable-windows" icon={<Monitor size={19} />} title={t("Windows")} description={t("Toggle proxy settings for the current user without command-line parameters.")}>
                <ScriptDownloadLink icon={<Monitor size={24} />} title={t("grouproxy-windows-setup.ps1")} subtitle={t("PowerShell script")} onDownload={() => downloadText(windowsScript.data, "grouproxy-windows-setup.ps1", "text/plain;charset=utf-8")} t={t} />
                <p className="access-doc-card-note">{t("After downloading, run it directly from PowerShell. Each run checks the current proxy state and switches it to the opposite state; then restart affected applications.")}</p>
              </PlatformCard>
              <PlatformCard id="access-reusable-macos" icon={<Laptop size={19} />} title={t("macOS")} description={t("Download the shortcut, import it into the Shortcuts app, and run it.")}>
                <MacOSShortcutLink href={macOSShortcut?.href} filename={macOSShortcut?.filename} title={t("Reusable setup script")} t={t} />
              </PlatformCard>
            </div>
          </section>
        </main>

        <aside className="access-docs-rail" aria-label={t("On this page")}>
          <nav className="access-docs-rail-sticky">
            <div className="access-docs-rail-title">{t("On this page")}</div>
            <div className="access-docs-rail-group">
              <a className="access-docs-rail-parent" href="#access-quick-start">{t("Quick start")}</a>
              <div className="access-docs-rail-children"><a href="#access-quick-linux">{t("Linux")}</a><a href="#access-quick-windows">{t("Windows")}</a><a href="#access-quick-macos">{t("macOS")}</a></div>
            </div>
            <div className="access-docs-rail-group">
              <a className="access-docs-rail-parent" href="#access-testing">{t("Testing and validation")}</a>
              <div className="access-docs-rail-children"><a href="#access-testing-linux">{t("Linux")}</a><a href="#access-testing-windows">{t("Windows")}</a><a href="#access-testing-macos">{t("macOS")}</a></div>
            </div>
            <div className="access-docs-rail-group">
              <a className="access-docs-rail-parent" href="#access-reusable-scripts">{t("Reusable scripts")}</a>
              <div className="access-docs-rail-children"><a href="#access-reusable-linux">{t("Linux")}</a><a href="#access-reusable-windows">{t("Windows")}</a><a href="#access-reusable-macos">{t("macOS")}</a></div>
            </div>
            <div className="access-docs-rail-endpoint"><span>{t("Environment")}</span><strong>{t(config.environment === "test" ? "Test environment" : "Production environment")}</strong><code>{config.fqdn}:{config.port}</code></div>
          </nav>
        </aside>
      </div>
    </div>
  );
}
