import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const typescript = require("typescript");
const source = await readFile(new URL("../lib/utils.ts", import.meta.url), "utf8");
const transpiled = typescript.transpileModule(source, {
  compilerOptions: {
    module: typescript.ModuleKind.ESNext,
    target: typescript.ScriptTarget.ES2022,
  },
}).outputText;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(transpiled).toString("base64")}`;
const { createFormatters } = await import(moduleUrl);
const releaseEventSource = await readFile(new URL("../lib/release-events.ts", import.meta.url), "utf8");
const releaseEventModule = typescript.transpileModule(releaseEventSource, {
  compilerOptions: {
    module: typescript.ModuleKind.ESNext,
    target: typescript.ScriptTarget.ES2022,
  },
}).outputText;
const releaseEventModuleUrl = `data:text/javascript;base64,${Buffer.from(releaseEventModule).toString("base64")}`;
const { formatReleaseEventMessage, formatReleaseEventSource } = await import(releaseEventModuleUrl);

// This is the same canonical shape the API returns. Formatting it against
// different locale-bound views must not mutate it or require another request.
const telemetry = Object.freeze({
  sampled_at: "2026-08-29T12:34:56.000Z",
  bytes_down: 1_536_000,
  latency_ms: 1_500,
  active_connections: 12_345.67,
});

const zh = createFormatters("zh-CN");
const en = createFormatters("en");
const es = createFormatters("es");

const chineseView = [
  zh.formatDate(telemetry.sampled_at),
  zh.formatBytes(telemetry.bytes_down),
  zh.formatDuration(telemetry.latency_ms),
  zh.formatNumber(telemetry.active_connections),
];
const englishView = [
  en.formatDate(telemetry.sampled_at),
  en.formatBytes(telemetry.bytes_down),
  en.formatDuration(telemetry.latency_ms),
  en.formatNumber(telemetry.active_connections),
];
const spanishView = [
  es.formatDate(telemetry.sampled_at),
  es.formatBytes(telemetry.bytes_down),
  es.formatDuration(telemetry.latency_ms),
  es.formatNumber(telemetry.active_connections),
];

assert.notDeepEqual(chineseView, englishView);
assert.notDeepEqual(englishView, spanishView);
assert.deepEqual(telemetry, {
  sampled_at: "2026-08-29T12:34:56.000Z",
  bytes_down: 1_536_000,
  latency_ms: 1_500,
  active_connections: 12_345.67,
});

const zhMessages = {
  "succeeded": "已完成",
  "health_check": "健康检查",
  "pass": "通过",
  "fail": "失败",
  "Release {releaseId} created for {count} nodes.": "已为 {count} 个节点创建发布 {releaseId}。",
  "Task {taskId} entered {status} state.": "任务 {taskId} 已进入“{status}”状态。",
  "Node acknowledgement received: {stage}; sing-box={singbox}, nftables={nftables}, health={health}.": "已收到节点确认：{stage}；sing-box={singbox}，nftables={nftables}，健康检查={health}。",
  "Release completed with status {status}.": "发布已完成，状态为“{status}”。",
  "Release orchestrator": "发布编排器",
  "Task worker": "任务处理器",
  "Release coordinator": "发布协调器",
  "Node agent: {node}": "节点代理：{node}",
};
const translate = (key, values = {}) => Object.entries(values).reduce(
  (message, [name, value]) => message.replaceAll(`{${name}}`, String(value)),
  zhMessages[key] || key,
);

assert.equal(
  formatReleaseEventMessage("Release release-123 created for 2 node(s).", translate),
  "已为 2 个节点创建发布 release-123。",
);
assert.equal(
  formatReleaseEventMessage("Task task-456 entered succeeded state.", translate),
  "任务 task-456 已进入“已完成”状态。",
);
assert.equal(
  formatReleaseEventMessage("Node ACK received: health_check; sing-box=pass, nftables=pass, health=fail.", translate),
  "已收到节点确认：健康检查；sing-box=通过，nftables=通过，健康检查=失败。",
);
assert.equal(
  formatReleaseEventMessage("Release completed with status succeeded.", translate), "发布已完成，状态为“已完成”。");
assert.equal(formatReleaseEventMessage("Unknown operational event.", translate), "Unknown operational event.");
assert.equal(formatReleaseEventSource("orchestrator", translate), "发布编排器");
assert.equal(formatReleaseEventSource("task", translate), "任务处理器");
assert.equal(formatReleaseEventSource("agent:nuc", translate), "节点代理：nuc");
assert.equal(formatReleaseEventSource("custom-source", translate), "custom-source");

console.log("Locale formatters and release events render canonical API payloads without mutation.");
