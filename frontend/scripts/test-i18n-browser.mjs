import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const frontendURL = process.env.GROUPROXY_BROWSER_FRONTEND_URL || "http://127.0.0.1:3000";
const backendURL = process.env.GROUPROXY_BROWSER_BACKEND_URL || "http://127.0.0.1:8000";
const browserPath = process.env.GROUPROXY_BROWSER_PATH || "/connections";
const itcode = process.env.GROUPROXY_BROWSER_ITCODE;
const password = process.env.GROUPROXY_BROWSER_PASSWORD;
const chromeBin = process.env.CHROME_BIN;
const screenshotPath = process.env.GROUPROXY_BROWSER_SCREENSHOT;
const viewport = process.env.GROUPROXY_BROWSER_VIEWPORT || "1440x900";
const readySelector = process.env.GROUPROXY_BROWSER_READY_SELECTOR || "tbody tr";
const smokeOnly = process.env.GROUPROXY_BROWSER_SMOKE_ONLY === "1";

if (!itcode || !password) {
  throw new Error("GROUPROXY_BROWSER_ITCODE and GROUPROXY_BROWSER_PASSWORD are required");
}
if (!chromeBin) throw new Error("CHROME_BIN is required");
const viewportMatch = viewport.match(/^(\d{2,4})x(\d{2,4})$/);
if (!viewportMatch) throw new Error("GROUPROXY_BROWSER_VIEWPORT must use WIDTHxHEIGHT");
const viewportWidth = Number(viewportMatch[1]);
const viewportHeight = Number(viewportMatch[2]);

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function stopChrome(chrome) {
  if (!chrome || chrome.exitCode !== null || chrome.signalCode !== null) return;
  const exited = new Promise((resolve) => chrome.once("exit", resolve));
  chrome.kill("SIGTERM");
  await Promise.race([exited, sleep(5_000)]);
  if (chrome.exitCode === null && chrome.signalCode === null) {
    chrome.kill("SIGKILL");
    await Promise.race([exited, sleep(2_000)]);
  }
}

class CdpClient {
  constructor(endpoint) {
    this.socket = new WebSocket(endpoint);
    this.nextId = 0;
    this.pending = new Map();
    this.events = new Map();
    this.ready = new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", reject, { once: true });
    });
    this.socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(`${message.error.code}: ${message.error.message}`));
        else pending.resolve(message.result);
        return;
      }
      for (const listener of this.events.get(message.method) || []) listener(message.params);
    });
  }

  on(method, listener) {
    const listeners = this.events.get(method) || [];
    listeners.push(listener);
    this.events.set(method, listeners);
  }

  async send(method, params = {}) {
    await this.ready;
    const id = ++this.nextId;
    this.socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  close() {
    this.socket.close();
  }
}

function startChrome(profileDirectory) {
  const chrome = spawn(
    chromeBin,
    [
      "--headless=new",
      "--no-sandbox",
      "--disable-gpu",
      "--remote-debugging-address=127.0.0.1",
      "--remote-debugging-port=0",
      `--user-data-dir=${profileDirectory}`,
      "about:blank",
    ],
    {
      env: { HOME: profileDirectory, PATH: process.env.PATH || "" },
      stdio: ["ignore", "ignore", "pipe"],
    },
  );
  const endpoint = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Chrome DevTools did not start")), 15_000);
    chrome.stderr.on("data", (chunk) => {
      const match = String(chunk).match(/DevTools listening on (ws:\/\/\S+)/);
      if (!match) return;
      clearTimeout(timeout);
      resolve(match[1]);
    });
    chrome.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    chrome.once("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`Chrome exited before DevTools became available (${code ?? "unknown"})`));
    });
  });
  return { chrome, endpoint };
}

async function waitFor(check, description) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (await check()) return;
    await sleep(100);
  }
  throw new Error(`Timed out waiting for ${description}`);
}

function readValue(result) {
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "Runtime evaluation failed");
  return result.result.value;
}

async function login() {
  const response = await fetch(`${backendURL}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ itcode, password }),
  });
  if (!response.ok) throw new Error(`Browser test login failed with HTTP ${response.status}`);
  const payload = await response.json();
  if (!payload.access_token) throw new Error("Browser test login did not return a session token");
  return payload.access_token;
}

let token = "";
let chrome;
let client;
let profileDirectory;

try {
  token = await login();
  profileDirectory = await mkdtemp(path.join(os.tmpdir(), "grouproxy-i18n-browser-"));
  const launched = startChrome(profileDirectory);
  chrome = launched.chrome;
  const browserEndpoint = await launched.endpoint;
  const debuggerURL = new URL(browserEndpoint);
  const targets = await fetch(`http://${debuggerURL.host}/json/list`).then((response) => response.json());
  const page = targets.find((target) => target.type === "page");
  if (!page?.webSocketDebuggerUrl) throw new Error("Chrome did not expose a page target");

  client = new CdpClient(page.webSocketDebuggerUrl);
  const apiRequests = [];
  client.on("Network.requestWillBeSent", ({ request }) => {
    if (request.url.includes("/backend-api/")) apiRequests.push(request.url);
  });
  await client.send("Page.enable");
  await client.send("Network.enable");
  await client.send("Emulation.setDeviceMetricsOverride", {
    width: viewportWidth,
    height: viewportHeight,
    deviceScaleFactor: 1,
    mobile: viewportWidth < 600,
  });
  await client.send("Page.addScriptToEvaluateOnNewDocument", {
    source: `localStorage.setItem("grouproxy.management_token", ${JSON.stringify(token)}); localStorage.setItem("grouproxy.session_role", "admin");`,
  });
  await client.send("Page.navigate", { url: `${frontendURL}${browserPath}` });

  const evaluate = async (expression) => {
    const result = await client.send("Runtime.evaluate", { expression, returnByValue: true });
    return readValue(result);
  };
  await waitFor(
    () => evaluate(`document.querySelector(${JSON.stringify(readySelector)}) !== null`),
    `page element ${readySelector}`,
  );
  await waitFor(
    () => evaluate("document.documentElement.lang === 'zh-CN'"),
    "initial Chinese locale",
  );

  if (smokeOnly) {
    const visualState = await evaluate(`(() => {
      const target = document.querySelector(${JSON.stringify(readySelector)});
      return {
        stylesheets: document.styleSheets.length,
        targetDisplay: target ? getComputedStyle(target).display : "none",
        bodyFont: getComputedStyle(document.body).fontFamily,
      };
    })()`);
    assert.ok(visualState.stylesheets > 0, "The page did not load a stylesheet");
    assert.notEqual(visualState.targetDisplay, "none", "The ready element is not visible");
    assert.match(visualState.bodyFont, /Euclid|Inter|Segoe UI/, "The application font stack is missing");
    console.log(`Rendered ${browserPath} with ${visualState.stylesheets} stylesheet(s).`);
  } else {
    const before = await evaluate(
      "[...document.querySelectorAll('tbody tr:first-child td')].map((cell) => cell.innerText)",
    );
    const requestCountBeforeSwitch = apiRequests.length;

    async function switchLocale(locale) {
      await evaluate(`(() => {
        const select = document.querySelector('.preferences-controls select');
        if (!select) throw new Error('Locale selector is missing');
        select.value = ${JSON.stringify(locale)};
        select.dispatchEvent(new Event('change', { bubbles: true }));
      })()`);
      await waitFor(() => evaluate(`document.documentElement.lang === ${JSON.stringify(locale)}`), locale);
      await sleep(300);
    }

    await switchLocale("en");
    const english = await evaluate(
      "[...document.querySelectorAll('tbody tr:first-child td')].map((cell) => cell.innerText)",
    );
    await switchLocale("es");
    const spanish = await evaluate(
      "[...document.querySelectorAll('tbody tr:first-child td')].map((cell) => cell.innerText)",
    );

    assert.notDeepEqual(before, english, "Chinese telemetry presentation did not update to English");
    assert.notDeepEqual(english, spanish, "English telemetry presentation did not update to Spanish");
    assert.equal(
      apiRequests.length,
      requestCountBeforeSwitch,
      "Changing locale unexpectedly refetched /backend-api data",
    );
    console.log("Locale switch updated cached telemetry in Chinese, English, and Spanish without an API refetch.");
  }
  if (screenshotPath) {
    const screenshot = await client.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(screenshotPath, Buffer.from(screenshot.data, "base64"));
  }
} finally {
  if (token) {
    await fetch(`${backendURL}/api/v1/auth/logout`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    }).catch(() => undefined);
  }
  client?.close();
  await stopChrome(chrome);
  // Chromium can flush profile files a moment after its main process exits.
  // Retrying prevents cleanup from hiding a passing browser assertion.
  if (profileDirectory) {
    await rm(profileDirectory, {
      recursive: true,
      force: true,
      maxRetries: 5,
      retryDelay: 150,
    });
  }
}
