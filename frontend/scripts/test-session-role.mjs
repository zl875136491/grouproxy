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
const sessionStorage = new Map();
let redirectedTo = "";
globalThis.window = {
  localStorage: {
    getItem: (key) => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  },
  sessionStorage: {
    getItem: (key) => sessionStorage.get(key) || null,
    setItem: (key, value) => sessionStorage.set(key, value),
    removeItem: (key) => sessionStorage.delete(key),
  },
  location: {
    pathname: "/nodes",
    replace: (value) => { redirectedTo = value; },
  },
};

const moduleUrl = `data:text/javascript;base64,${Buffer.from(transpiled).toString("base64")}`;
const {
  clearManagementSession,
  consumeAuthenticationNotice,
  hasAuthenticatedSession,
  hasManagementSession,
  managementSessionRole,
  saveManagementSession,
} = await import(moduleUrl);

saveManagementSession("employee-token", "employee", "2099-01-01T00:00:00Z");
assert.equal(hasAuthenticatedSession(), true);
assert.equal(hasManagementSession(), false);
assert.equal(managementSessionRole(), "employee");

clearManagementSession();
assert.equal(hasAuthenticatedSession(), false);
assert.equal(managementSessionRole(), null);

saveManagementSession("admin-token", "admin", "2099-01-01T00:00:00Z");
assert.equal(hasAuthenticatedSession(), true);
assert.equal(hasManagementSession(), true);
assert.equal(managementSessionRole(), "admin");

saveManagementSession("root-token", "root", "2099-01-01T00:00:00Z");
assert.equal(hasAuthenticatedSession(), true);
assert.equal(hasManagementSession(), true);
assert.equal(managementSessionRole(), "root");

saveManagementSession("expired-token", "admin", "2000-01-01T00:00:00Z");
assert.equal(hasManagementSession(), false);
assert.equal(managementSessionRole(), null);
assert.equal(redirectedTo, "/login?reason=management_session_expired");
assert.equal(consumeAuthenticationNotice(), "management_session_expired");
assert.equal(consumeAuthenticationNotice(), "");

window.location.pathname = "/";
redirectedTo = "";
saveManagementSession("expired-public-token", "admin", "2000-01-01T00:00:00Z");
assert.equal(hasAuthenticatedSession(), false);
assert.equal(redirectedTo, "");
assert.equal(managementSessionRole(), null);

console.log("Session roles and client-side expiration handling are valid.");
