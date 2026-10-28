"use client";

import { useMutation } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { useRef, useState, type ChangeEvent } from "react";
import { Button } from "../../components/ui";
import { previewRoutingImport, type RoutingPolicy } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import styles from "./routing.module.css";

const maxFileBytes = 1024 * 1024;

export function RoutingImport({ policy, onApply }: { policy: RoutingPolicy; onApply: (policy: RoutingPolicy) => void }) {
  const { t } = usePreferences();
  const [content, setContent] = useState("");
  const [mode, setMode] = useState<"append" | "replace">("append");
  const [fileError, setFileError] = useState("");
  const [reading, setReading] = useState(false);
  const [fileName, setFileName] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const [applied, setApplied] = useState(false);
  const fingerprint = JSON.stringify({ content, mode, policy });
  const preview = useMutation({
    mutationFn: async () => {
      const snapshot = fingerprint;
      const response = await previewRoutingImport(content, mode, policy);
      return { ...response, fingerprint: snapshot };
    },
  });
  const result = preview.data?.fingerprint === fingerprint ? preview.data : undefined;
  const busy = reading || preview.isPending;
  const updateContent = (value: string) => {
    setContent(value);
    setFileName("");
    setApplied(false);
    setFileError("");
    preview.reset();
  };
  const readFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    preview.reset();
    setApplied(false);
    if (file.size > maxFileBytes) {
      setFileError("Routing import files must not exceed 1 MiB.");
      return;
    }
    setReading(true);
    setFileError("");
    try {
      updateContent(await file.text());
      setFileName(file.name);
    } catch {
      setFileError("Unable to read the routing file.");
    } finally {
      setReading(false);
    }
  };
  return <section className={styles.importer} aria-label={t("Import sing-box rules")}>
    <h3>{t("Import sing-box rules")}</h3>
    <p>{t("Upload or paste UTF-8 JSON: a full sing-box configuration, a route object, or a rules array. Only routing rules and route.final are imported; node credentials, DNS and other settings are ignored.")}</p>
    <p>{t("Supported matchers: domain, domain_suffix, ip_cidr. Actions: route with direct / subscription / proxy, or reject. Unsupported matchers, logical rules and rule-set references are rejected, never silently skipped.")}</p>
    <div className={styles.importToolbar}>
      <div className={styles.importControls}>
        <div className={styles.fileControl}>
          <span id="routing-file-label">{t("Configuration file")}</span>
          <div className={styles.filePicker} role="group" aria-labelledby="routing-file-label">
            <input ref={fileInput} className={styles.fileInput} tabIndex={-1} aria-label={t("Configuration file")} type="file" accept=".json,application/json" disabled={busy} onChange={(event) => void readFile(event)} />
            <Button type="button" size="sm" disabled={busy} onClick={() => fileInput.current?.click()}>{t("Choose routing file")}</Button>
            <span className={styles.fileName} title={fileName || undefined}>{fileName || t("No routing file selected")}</span>
          </div>
        </div>
        <div className={styles.radioControl}>
          <span id="routing-import-mode-label">{t("Import behavior")}</span>
          <div className={styles.radioGroup} role="radiogroup" aria-labelledby="routing-import-mode-label">
            {(["append", "replace"] as const).map((behavior) => <label className={styles.radioOption} key={behavior}>
              <input type="radio" name="routing-import-mode" value={behavior} checked={mode === behavior} disabled={busy} onChange={() => { setMode(behavior); preview.reset(); setApplied(false); }} />
              {t(behavior === "append" ? "Append to custom rules" : "Replace custom rules")}
            </label>)}
          </div>
        </div>
      </div>
      <a className={`button button-secondary button-md ${styles.templateDownload}`} href="/templates/sing-box-routing.json" download><Download size={16} aria-hidden="true" />{t("Download sing-box template")}</a>
    </div>
    <label className={styles.importContent}>{t("Sing-box JSON configuration")}
      <textarea aria-label={t("Sing-box JSON configuration")} rows={9} maxLength={maxFileBytes} value={content} disabled={busy} spellCheck={false} onChange={(event) => updateContent(event.target.value)} placeholder={'{"route":{"rules":[{"domain_suffix":["example.com"],"action":"route","outbound":"direct"}],"final":"subscription"}}'} />
    </label>
    <div className={`form-actions ${styles.actions}`}>
      <Button disabled={busy || !content.trim()} onClick={() => { setApplied(false); preview.mutate(); }}>{t("Validate and preview import")}</Button>
      <Button disabled={busy || !result} onClick={() => {
        if (!result) return;
        onApply(result.policy);
        updateContent("");
        setApplied(true);
      }}>{t("Apply import to editor")}</Button>
    </div>
    {(fileError || preview.error) && <p role="alert">{t(fileError || preview.error?.message || "Operation failed")}</p>}
    {result && <div className={styles.importPreview} role="status">
      <p>{t("Import preview: {source} source rules expand to {imported} entries; {total} custom rules after import.", { source: result.source_rules, imported: result.imported_rules, total: result.policy.rules.length })}</p>
      <p>{t("Unmatched destinations")}: {t(result.policy.final === "proxy" ? "Proxy outbound" : "Direct outbound")}</p>
      <p>{t("Private and China direct settings remain unchanged. Applying only updates the editor; save and publish separately.")}</p>
      <details><summary>{t("View converted routing policy")}</summary><pre>{JSON.stringify(result.policy, null, 2)}</pre></details>
    </div>}
    {applied && <p role="status">{t("Import applied to the editor. Review the rules, then save and publish.")}</p>}
  </section>;
}
