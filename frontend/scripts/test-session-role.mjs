import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const typescript = require("typescript");
const source = await readFile(new URL("../lib/api.ts", import.meta.url), "utf8");
const transpiled = typescript.transpileModule(source, {
  compilerOptions: {
    module: typescript.ModuleKind.ESNext,
    target: typescript.ScriptTarget.ES2022,
  },
}).outputText;

const storage = new Map();
globalThis.window = {
  localStorage: {
    getItem: (key) => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  },
};

const moduleUrl = `data:text/javascript;base64,${Buffer.from(transpiled).toString("base64")}`;
const {
  clearManagementSession,
  hasAuthenticatedSession,
  hasManagementSession,
  managementSessionRole,
  saveManagementSession,
} = await import(moduleUrl);

saveManagementSession("employee-token", "employee");
assert.equal(hasAuthenticatedSession(), true);
assert.equal(hasManagementSession(), false);
assert.equal(managementSessionRole(), "employee");

clearManagementSession();
assert.equal(hasAuthenticatedSession(), false);
assert.equal(managementSessionRole(), null);

saveManagementSession("admin-token", "admin");
assert.equal(hasAuthenticatedSession(), true);
assert.equal(hasManagementSession(), true);
assert.equal(managementSessionRole(), "admin");

console.log("Employee sessions remain authenticated without receiving management UI access.");
