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

console.log("Locale formatters render one canonical API payload without mutation.");
