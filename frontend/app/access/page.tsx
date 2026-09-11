"use client";

import { useQuery } from "@tanstack/react-query";
import {
  BookOpen,
  Check,
  Clipboard,
  Clock3,
  Download,
  ExternalLink,
  FileCode2,
  Laptop,
  Monitor,
  Network,
  Terminal,
} from "lucide-react";
import Link from "next/link";
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
  onDownload?: () => void;
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

function CodeSnippet({ id, filename, language, content, copiedId, onCopy, onDownload }: CodeSnippetProps) {
  const copied = copiedId === id;
  return (
    <div className="access-doc-codeblock">
      <div className="access-doc-codebar">
        <div className="access-doc-code-meta"><FileCode2 size={14} aria-hidden="true" /><code>{filename}</code><span>{language}</span></div>
        <div className="row-actions">
          <IconButton className="access-doc-code-action" label={copied ? "Copied" : "Copy"} onClick={onCopy}>{copied ? <Check size={14} /> : <Clipboard size={14} />}</IconButton>
          {onDownload ? <IconButton className="access-doc-code-action" label="Download" onClick={onDownload}><Download size={14} /></IconButton> : null}
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

function MacOSShortcutLink({ href, title, unavailable = false, t }: { href?: string; title: string; unavailable?: boolean; t: (key: string, values?: Record<string, string | number>) => string }) {
  const content = <><img src="/apple-shortcuts-icon.jpg" alt="" width={44} height={44} /><span className="access-doc-shortcut-copy"><strong>{title}</strong><span>{t("macOS shortcut")}</span></span>{unavailable ? <Clock3 size={16} aria-hidden="true" /> : <ExternalLink size={16} aria-hidden="true" />}</>;
  if (unavailable) {
    return (
      <button className="access-doc-shortcut-link" type="button" onClick={() => notifyToast({ title: t("Feature in development") })} aria-label={`${title} - ${t("Feature in development")}`}>
        {content}
      </button>
    );
  }
  return (
    <a className="access-doc-shortcut-link" href={href} target="_blank" rel="noreferrer" aria-label={`${title} - ${t("macOS shortcut")}`}>
      {content}
    </a>
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

const macOSShortcutUrls = {
  quickAccess: {
    test: "https://www.icloud.com/shortcuts/d0b8a9e0e4a745de945cbc56d94424a8",
    production: "https://www.icloud.com/shortcuts/569e37fe985148449327b3e1e1c1f68e",
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
  const windowsQuickCommand = "Set-ExecutionPolicy -Scope Process Bypass -Force\n.\\grouproxy-windows-setup.ps1\n\n.\\grouproxy-windows-setup.ps1 -Disable";
  const linuxQuickCommand = "chmod +x ./grouproxy-linux-setup.sh\n./grouproxy-linux-setup.sh\n\n./grouproxy-linux-setup.sh --uninstall";
  const windowsValidationCommand = config ? `$ProxyUrl = \"${endpoint}\"\nTest-NetConnection -ComputerName \"${config.fqdn}\" -Port ${config.port}\nInvoke-RestMethod -Uri \"https://ipinfo.io/ip\" -Proxy $ProxyUrl` : "";
  const linuxValidationCommand = config ? `proxy=${shellQuote(endpoint)}\ncurl --connect-timeout 5 --fail --silent --show-error --proxy \"$proxy\" https://ipinfo.io/ip` : "";
  const windowsAllowlistCommand = config ? `$ProxyUrl = \"${endpoint}\"\n$SourceAddress = Invoke-RestMethod -Uri \"https://ipinfo.io/ip\" -Proxy $ProxyUrl\n$SourceAddress.Trim()` : "";
  const linuxAllowlistCommand = config ? `proxy=${shellQuote(endpoint)}\ncurl --connect-timeout 5 --fail --silent --show-error --proxy \"$proxy\" https://ipinfo.io/ip` : "";
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
              description="Connect your workstation, verify the route, and maintain source access from one place."
              icon={<BookOpen size={18} />}
            />
            <div className="access-endpoint-bar"><div className="access-endpoint-status"><StatusBadge status="enabled" /><span>{t(config.environment === "test" ? "Test environment" : "Production environment")}</span></div><code>{endpoint}</code><span className="access-endpoint-port">{t("Port")} {formatNumber(config.port, { useGrouping: false })}</span></div>
          </section>

          <section className="access-doc-section" aria-labelledby="access-quick-start">
            <SectionHeading id="access-quick-start" title={t("Quick start")} description={t("Choose an operating system and use the shortest supported setup path.")} />
            <div className="access-doc-platform-grid">
              <PlatformCard id="access-quick-linux" icon={<Terminal size={19} />} title={t("Linux")} description={t("Download the setup script and run it as the current user.")}>
                <CodeSnippet id="linux-quick" filename="grouproxy-linux-setup.sh" language="bash" content={linuxQuickCommand} copiedId={copiedId} onCopy={() => copySnippet("linux-quick", linuxQuickCommand, t("Linux"))} onDownload={() => downloadText(linuxScript.data, "grouproxy-linux-setup.sh", "text/x-shellscript")} />
              </PlatformCard>
              <PlatformCard id="access-quick-windows" icon={<Monitor size={19} />} title={t("Windows")} description={t("Use the downloaded script to turn the current user's Windows system proxy on or off.")}>
                <CodeSnippet id="windows-quick" filename="grouproxy-windows-setup.ps1" language="powershell" content={windowsQuickCommand} copiedId={copiedId} onCopy={() => copySnippet("windows-quick", windowsQuickCommand, t("Windows"))} onDownload={() => downloadText(windowsScript.data, "grouproxy-windows-setup.ps1", "text/plain;charset=utf-8")} />
                <p className="access-doc-card-note">{t("Run without options to enable the proxy. Run with -Disable to turn it off.")}</p>
              </PlatformCard>
              <PlatformCard id="access-quick-macos" icon={<Laptop size={19} />} title={t("macOS")} description={t("Use the shortcut prepared for the selected deployment environment.")}>
                <MacOSShortcutLink href={macOSShortcutUrls.quickAccess[config.environment]} title={t("Enable proxy script")} t={t} />
              </PlatformCard>
            </div>
          </section>

          <section className="access-doc-section" aria-labelledby="access-testing">
            <SectionHeading id="access-testing" title={t("Testing and validation")} description={t("Confirm the listener and proxy route, then use the returned address to maintain source access.")} />
            <div className="access-doc-platform-grid">
              <PlatformCard id="access-testing-linux" icon={<Terminal size={19} />} title={t("Linux")} description={t("Check the listener and return the address seen through the proxy.")}>
                <CodeSnippet id="linux-validation" filename="grouproxy-check.sh" language="bash" content={linuxValidationCommand} copiedId={copiedId} onCopy={() => copySnippet("linux-validation", linuxValidationCommand, t("Linux"))} />
              </PlatformCard>
              <PlatformCard id="access-testing-windows" icon={<Monitor size={19} />} title={t("Windows")} description={t("Check the listener and return the address seen through the proxy.")}>
                <CodeSnippet id="windows-validation" filename="grouproxy-check.ps1" language="powershell" content={windowsValidationCommand} copiedId={copiedId} onCopy={() => copySnippet("windows-validation", windowsValidationCommand, t("Windows"))} />
              </PlatformCard>
              <PlatformCard id="access-testing-macos" icon={<Laptop size={19} />} title={t("macOS")} description={t("This macOS shortcut is in development.")}>
                <MacOSShortcutLink unavailable title={t("Test the actual proxy route")} t={t} />
              </PlatformCard>
            </div>
          </section>

          <section className="access-doc-section" aria-labelledby="access-allowlist">
            <SectionHeading id="access-allowlist" title={t("Allowlist")} description={t("Use the returned address to locate or add the matching source CIDR.")} />
            <div className="access-doc-platform-grid">
              <PlatformCard id="access-allowlist-linux" icon={<Terminal size={19} />} title={t("Linux")} description={t("Collect the proxy source address, then open site policy to maintain the allowlist.")}>
                <CodeSnippet id="linux-allowlist" filename="grouproxy-source-address.sh" language="bash" content={linuxAllowlistCommand} copiedId={copiedId} onCopy={() => copySnippet("linux-allowlist", linuxAllowlistCommand, t("Linux"))} />
                <Link className="access-doc-card-link" href="/sites"><Network size={15} aria-hidden="true" />{t("Manage source CIDRs")}</Link>
              </PlatformCard>
              <PlatformCard id="access-allowlist-windows" icon={<Monitor size={19} />} title={t("Windows")} description={t("Collect the proxy source address, then open site policy to maintain the allowlist.")}>
                <CodeSnippet id="windows-allowlist" filename="grouproxy-source-address.ps1" language="powershell" content={windowsAllowlistCommand} copiedId={copiedId} onCopy={() => copySnippet("windows-allowlist", windowsAllowlistCommand, t("Windows"))} />
                <Link className="access-doc-card-link" href="/sites"><Network size={15} aria-hidden="true" />{t("Manage source CIDRs")}</Link>
              </PlatformCard>
              <PlatformCard id="access-allowlist-macos" icon={<Laptop size={19} />} title={t("macOS")} description={t("This macOS shortcut is in development.")}>
                <MacOSShortcutLink unavailable title={t("Manage source CIDRs")} t={t} />
                <Link className="access-doc-card-link" href="/sites"><Network size={15} aria-hidden="true" />{t("Manage source CIDRs")}</Link>
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
              <a className="access-docs-rail-parent" href="#access-allowlist">{t("Allowlist")}</a>
              <div className="access-docs-rail-children"><a href="#access-allowlist-linux">{t("Linux")}</a><a href="#access-allowlist-windows">{t("Windows")}</a><a href="#access-allowlist-macos">{t("macOS")}</a></div>
            </div>
            <div className="access-docs-rail-endpoint"><span>{t("Environment")}</span><strong>{t(config.environment === "test" ? "Test environment" : "Production environment")}</strong><code>{config.fqdn}:{config.port}</code></div>
          </nav>
        </aside>
      </div>
    </div>
  );
}
