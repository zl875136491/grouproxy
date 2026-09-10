import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const typescript = require("typescript");
const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const projectRoot = path.resolve(frontendRoot, "..");

const localizedComponentProps = {
  ConfirmDialog: ["title", "description", "confirmLabel"],
  DetailDialog: ["title", "description"],
  EmptyState: ["title", "detail"],
  ErrorState: ["error"],
  IconButton: ["label"],
  PageHeader: ["eyebrow", "title", "description"],
  RefreshButton: ["label"],
  SectionHeading: ["eyebrow", "title", "description"],
  StatusBadge: ["status"],
};

// These are protocol names, product names, or commands rather than UI copy.
const allowedRawJsxText = new Set([
  "CIDR",
  "GROUPROXY",
  "Grouproxy",
  "HTTP",
  "HTTP :1080",
  "Set-ExecutionPolicy -Scope Process Bypass",
  "VLESS / VMess",
  "YAML / JSON",
  ".\\grouproxy-windows-setup.ps1",
  "sing-box",
  "sing-box · nftables ·",
]);

function propertyName(node) {
  if (typescript.isIdentifier(node) || typescript.isStringLiteral(node) || typescript.isNoSubstitutionTemplateLiteral(node)) {
    return node.text;
  }
  return undefined;
}

async function sourceFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await sourceFiles(entryPath));
    else if (entry.name.endsWith(".tsx") || entry.name.endsWith(".ts")) files.push(entryPath);
  }
  return files;
}

function collectMessages(object, messages) {
  for (const property of object.properties) {
    if (!typescript.isPropertyAssignment(property)) continue;
    const key = propertyName(property.name);
    if (key && isStringLiteral(property.initializer)) messages.set(key, property.initializer.text);
  }
}

function messageLocale(target, sourceFile) {
  if (
    typescript.isElementAccessExpression(target)
    && target.expression.getText(sourceFile) === "messages"
    && target.argumentExpression
  ) {
    return propertyName(target.argumentExpression);
  }
  if (typescript.isPropertyAccessExpression(target) && target.expression.getText(sourceFile) === "messages") {
    return target.name.text;
  }
  return undefined;
}

async function readChineseMessages() {
  const preferencesPath = path.join(frontendRoot, "lib", "preferences.tsx");
  const source = typescript.createSourceFile(
    preferencesPath,
    await readFile(preferencesPath, "utf8"),
    typescript.ScriptTarget.Latest,
    true,
    typescript.ScriptKind.TSX,
  );
  const messages = new Map();

  function visit(node) {
    if (
      typescript.isVariableDeclaration(node)
      && typescript.isIdentifier(node.name)
      && node.name.text === "messages"
      && node.initializer
      && typescript.isObjectLiteralExpression(node.initializer)
    ) {
      for (const property of node.initializer.properties) {
        if (
          typescript.isPropertyAssignment(property)
          && propertyName(property.name) === "zh-CN"
          && typescript.isObjectLiteralExpression(property.initializer)
        ) {
          collectMessages(property.initializer, messages);
        }
      }
    }

    if (
      typescript.isCallExpression(node)
      && typescript.isPropertyAccessExpression(node.expression)
      && node.expression.expression.getText(source) === "Object"
      && node.expression.name.text === "assign"
      && node.arguments.length >= 2
      && messageLocale(node.arguments[0], source) === "zh-CN"
      && typescript.isObjectLiteralExpression(node.arguments[1])
    ) {
      collectMessages(node.arguments[1], messages);
    }

    typescript.forEachChild(node, visit);
  }

  visit(source);
  return messages;
}

function isStringLiteral(node) {
  return typescript.isStringLiteral(node) || typescript.isNoSubstitutionTemplateLiteral(node);
}

async function interfaceCopy() {
  const files = [
    ...await sourceFiles(path.join(frontendRoot, "app")),
    ...await sourceFiles(path.join(frontendRoot, "components")),
    path.join(frontendRoot, "lib", "release-events.ts"),
  ];
  const keys = [];
  const rawText = [];

  for (const file of files) {
    const source = typescript.createSourceFile(
      file,
      await readFile(file, "utf8"),
      typescript.ScriptTarget.Latest,
      true,
      typescript.ScriptKind.TSX,
    );
    const location = (node) => `${path.relative(projectRoot, file)}:${source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1}`;
    const add = (key, node) => keys.push({ key, location: location(node) });

    function visit(node) {
      if (
        typescript.isCallExpression(node)
        && typescript.isIdentifier(node.expression)
        && node.expression.text === "t"
        && node.arguments[0]
        && isStringLiteral(node.arguments[0])
      ) {
        add(node.arguments[0].text, node);
      }

      if (typescript.isJsxOpeningElement(node) || typescript.isJsxSelfClosingElement(node)) {
        const component = node.tagName.getText(source);
        for (const attribute of node.attributes.properties) {
          if (!typescript.isJsxAttribute(attribute) || !attribute.initializer || !isStringLiteral(attribute.initializer)) continue;
          if (localizedComponentProps[component]?.includes(attribute.name.text)) add(attribute.initializer.text, attribute);
          if (["aria-label", "title"].includes(attribute.name.text)) add(attribute.initializer.text, attribute);
        }
      }

      if (
        typescript.isCallExpression(node)
        && typescript.isIdentifier(node.expression)
        && ["notifyToast", "toast"].includes(node.expression.text)
        && node.arguments[0]
        && typescript.isObjectLiteralExpression(node.arguments[0])
      ) {
        for (const property of node.arguments[0].properties) {
          if (
            typescript.isPropertyAssignment(property)
            && ["title", "description"].includes(propertyName(property.name))
            && isStringLiteral(property.initializer)
          ) {
            add(property.initializer.text, property);
          }
        }
      }

      if (typescript.isJsxText(node)) {
        const value = node.getText(source).trim();
        if (/[A-Za-z]{2,}/.test(value) && !allowedRawJsxText.has(value)) {
          rawText.push({ value, location: location(node) });
        }
      }

      typescript.forEachChild(node, visit);
    }

    visit(source);
  }

  return { keys, rawText };
}

const operationalRuntimeKeys = [
  "success",
  "archived",
  "pass",
  "fail",
  "cancelled",
  "dead_letter",
  "retry_scheduled",
  "probing",
  "unknown",
  "not_configured",
  "probe_failed",
  "proxy_config_unavailable",
  "half_open_probe_failed",
  "failure_threshold",
  "cooldown_elapsed",
  "recovery_success_threshold",
  "refresh_failed",
  "apply_failed",
  "rollback_failed",
  "config.publish",
  "subscription.refresh",
  "node.probe",
  "backup.create",
  "backup.restore",
  "auth.verification.request",
  "auth.register",
  "auth.password.change",
  "auth.login.password",
  "auth.login.gquan",
  "auth.logout",
  "site.rename",
  "site.shutdown",
  "site.restore",
  "node.create",
  "node.rename",
  "cidr.create",
  "cidr.delete",
  "exception.create",
  "exception.delete",
  "cross_site.update",
  "blacklist.create",
  "blacklist.delete",
  "subscription_source.create",
  "subscription.upload",
  "subscription.single_node.create",
  "config_draft.create",
  "config_release.create",
  "subscription.publish",
  "subscription.rollback",
  "task.cancel",
  "proxy_selection.update",
  "node.probe.create",
  "agent.logs.ingest",
  "audit.export",
  "backup.create.request",
  "backup.restore.request",
  "subscription.refresh.failed",
  "backup.schedule",
  "backup.rehearsal.schedule",
  "backup.retention.delete",
  "backup.restore.rehearsal",
  "backup.failed",
  "admin_user",
  "site",
  "node",
  "site_cidr",
  "travel_exception",
  "cross_site_allow",
  "destination_blacklist",
  "subscription_source",
  "subscription_version",
  "config_draft",
  "config_release",
  "task",
  "audit",
  "backup",
  "task_lease_expired",
  "subscription_refresh_unexpected_error",
  "subscription_ssrf_blocked",
  "subscription_clash_invalid",
  "subscription_outbound_type_invalid",
  "Backup maintenance failed",
  "Backup verification failed",
  "The automatic backup maintenance cycle did not complete.",
];

function normalizedMessageKey(key) {
  return key.replaceAll("_", " ").replaceAll(".", " ");
}

const [chineseMessages, { keys: usedCopy, rawText }, preferencesSource, taskPageSource, auditPageSource, uiSource] = await Promise.all([
  readChineseMessages(),
  interfaceCopy(),
  readFile(path.join(frontendRoot, "lib", "preferences.tsx"), "utf8"),
  readFile(path.join(frontendRoot, "app", "tasks", "page.tsx"), "utf8"),
  readFile(path.join(frontendRoot, "app", "audit", "page.tsx"), "utf8"),
  readFile(path.join(frontendRoot, "components", "ui.tsx"), "utf8"),
]);
const missing = [...new Map(usedCopy.map((entry) => [entry.key, entry])).values()].filter((entry) => !chineseMessages.has(entry.key));
const untranslatedRuntimeValues = operationalRuntimeKeys.filter((key) => {
  const message = chineseMessages.get(key) || chineseMessages.get(normalizedMessageKey(key));
  return !message || !/[\u3400-\u9fff]/.test(message);
});

assert.deepEqual(missing, [], `Missing Chinese UI translations:\n${missing.map((entry) => `${entry.location}: ${entry.key}`).join("\n")}`);
assert.deepEqual(rawText, [], `Unexpected raw English JSX text:\n${rawText.map((entry) => `${entry.location}: ${entry.value}`).join("\n")}`);
assert.deepEqual(untranslatedRuntimeValues, [], `Missing Chinese runtime translations:\n${untranslatedRuntimeValues.join("\n")}`);
assert.match(preferencesSource, /replaceAll\("_", " "\)\.replaceAll\("\.", " "\)/, "Dotted API values must be normalized before lookup.");
assert.match(taskPageSource, /\bt\(task\.task_type\)/, "Task types must use their API key for translation.");
assert.match(taskPageSource, /\bt\(task\.stage\)/, "Task stages must use their API key for translation.");
assert.doesNotMatch(taskPageSource, /label\(task\.task_type\)/, "Task types must not be title-cased before translation.");
assert.match(auditPageSource, /\bt\(event\.action\)/, "Audit actions must be translated.");
assert.match(auditPageSource, /\bt\(event\.target_type\)/, "Audit target types must be translated.");
assert.match(auditPageSource, /\bt\(selected\.target_type\)/, "Audit detail target types must be translated.");
assert.match(uiSource, /\bt\(status\)/, "Status badges must use the original API status key.");

console.log(`Chinese UI coverage verified for ${usedCopy.length} localized usages and ${operationalRuntimeKeys.length} runtime values.`);
