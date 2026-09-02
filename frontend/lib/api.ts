export type Overview = {
  sites: number;
  nodes: number;
  online_nodes: number;
  in_sync_nodes: number;
  drifted_nodes: number;
  connections: number;
  open_circuits: number;
  open_alerts: number;
  http_only: boolean;
};

export type VerificationPurpose = "register" | "password_change" | "gquan_login";

export type AuthSession = {
  access_token: string;
  token_type: "bearer";
  itcode: string;
  role: "admin" | "employee";
  expires_at: string;
};

export type VerificationChallenge = {
  challenge_id: string;
  expires_at: string;
  resend_available_at: string;
};

export type Site = {
  id: string;
  slug: string;
  name: string;
  dns_note: string;
  proxy_auth_required: boolean;
  shutdown: boolean;
  config_revision: number;
  http_port: number;
};

export type Node = {
  id: string;
  site_id: string;
  name: string;
  agent_id: string;
  advertise_ip: string;
  monitor_version: string;
  singbox_version: string;
  last_seen_at: string | null;
  desired_version: number;
  applied_version: number;
  applied_hash: string;
  liveness_status: string;
  config_status: string;
  service_status: string;
  subscription_status: string;
  probe_status: string;
  last_error: string;
};

export type ProxyHistoryPoint = {
  at: string | null;
  delay_ms: number | null;
};

export type ProxyEndpoint = {
  name: string;
  type: string;
  udp: boolean;
  alive: boolean | null;
  delay_ms: number | null;
  history: ProxyHistoryPoint[];
};

export type ProxyGroup = {
  name: string;
  type: string;
  now: string;
  all: string[];
  nodes: ProxyEndpoint[];
  udp: boolean;
  delay_ms: number | null;
  history: ProxyHistoryPoint[];
};

export type ProxyConfigSnapshot = {
  id: string;
  node_id: string;
  site_id: string;
  sampled_at: string;
  api_available: boolean;
  groups: ProxyGroup[];
  error: string;
  received_at: string;
};

export type ProxySelectionRequest = {
  group: string;
  outbound: string;
  expected_current_version?: number | null;
  note?: string;
};

export type CIDREntry = {
  id: string;
  site_id: string;
  cidr: string;
  comment: string;
  enabled: boolean;
};

export type CIDRPreview = {
  allowed: boolean;
  matched_cidr: string | null;
  requires_auth: boolean;
  reason: string;
  effective_cidrs: string[];
};

export type TravelException = {
  id: string;
  cidr: string;
  comment: string;
  owner: string;
  expires_at: string;
  enabled: boolean;
  created_at: string;
};

export type CrossSiteAllow = {
  id: string;
  from_site_id: string;
  to_site_id: string;
  enabled: boolean;
  comment: string;
  updated_at: string;
};

export type DestinationBlacklist = {
  id: string;
  pattern: string;
  kind: "domain" | "ip" | "cidr";
  comment: string;
  enabled: boolean;
  created_at: string;
};

export type SubscriptionSource = {
  id: string;
  name: string;
  url_hint: string;
  fetch_interval_sec: number;
  max_body_bytes: number;
  redirect_limit: number;
  enabled: boolean;
  refreshable: boolean;
  last_refresh_at: string | null;
  last_refresh_attempt_at: string | null;
  last_refresh_error: string;
  consecutive_failures: number;
  created_at: string;
  updated_at: string;
};

export type SubscriptionVersion = {
  id: string;
  source_id: string;
  version: number;
  content_hash: string;
  size_bytes: number;
  format: "clash" | "sip008" | "sing-box" | "unknown";
  fetched_at: string;
  parse_ok: boolean;
  parse_error: string;
  node_count: number;
  published: boolean;
  created_at: string;
};

export type SiteSubscription = {
  site_id: string;
  source_id: string;
  subscription_version_id: string;
  previous_subscription_version_id: string | null;
  updated_at: string;
};

export type SubscriptionCatalog = {
  sources: SubscriptionSource[];
  versions: SubscriptionVersion[];
  site_subscriptions: SiteSubscription[];
};

export type SubscriptionRefreshResponse = {
  source: SubscriptionSource;
  task: Task;
  merged: boolean;
};

export type SubscriptionUploadResponse = {
  source: SubscriptionSource;
  version: SubscriptionVersion;
};

export type SubscriptionPublishResponse = {
  version: SubscriptionVersion;
  releases: Release[];
};

export type Draft = {
  id: string;
  site_id: string;
  node_ids: string[];
  source_revision: number;
  diff: Record<string, unknown>;
  validation: {
    valid?: boolean;
    errors?: string[];
    effective_cidrs?: string[];
    acl_sources?: Record<string, string[]>;
  };
  risk_level: string;
  status: string;
  expires_at: string;
  created_at: string;
  updated_at: string;
};

export type Release = {
  release_id: string;
  site_id: string;
  node_ids: string[];
  desired_release_id: string;
  previous_release_id: string | null;
  task_id: string | null;
  status: string;
  stage: string;
  progress: number;
  error: string;
  rollback_reason: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

export type Task = {
  task_id: string;
  task_type: string;
  target_type: string;
  target_id: string;
  status: string;
  progress: number;
  stage: string;
  progress_message: string;
  retry_count: number;
  max_retries: number;
  error: string;
  result: Record<string, unknown>;
  created_at: string;
  finished_at: string | null;
  next_run_at: string;
  locked_by: string;
  lease_expires_at: string | null;
};

export type AgentAck = {
  node_id: string;
  release_id: string;
  desired_version: number;
  applied_version: number;
  bundle_hash: string;
  applied_hash: string;
  ok: boolean;
  singbox_ok: boolean;
  nft_ok: boolean;
  health_ok: boolean;
  rollback_attempted: boolean;
  rollback_ok: boolean;
  last_good_version: number;
  stage: string;
  error_code: string;
  error_message: string;
  sequence: number;
  received_at: string;
};

export type AuditEvent = {
  event_id: string;
  actor: string;
  actor_role: string;
  request_id: string;
  source_ip: string;
  action: string;
  target_type: string;
  target_id: string;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  result: string;
  error: string;
  immutable_hash: string;
  previous_hash: string;
  at: string;
};

export type AccessLog = {
  id: string;
  ts: string;
  site_id: string;
  node_id: string;
  policy_version: number;
  src_ip: string;
  src_cidr_match: string;
  username: string;
  cert_fp: string;
  dst_host: string;
  dst_port: number;
  action: "allow" | "deny";
  deny_reason: string;
  bytes_up: number;
  bytes_down: number;
  duration_ms: number;
};

export type ConnectionTopItem = {
  label: string;
  connections: number;
  bytes_up: number;
  bytes_down: number;
};

export type ConnectionSnapshot = {
  id: string;
  node_id: string;
  site_id: string;
  sampled_at: string;
  active_connections: number;
  bytes_up: number;
  bytes_down: number;
  top_sources: ConnectionTopItem[];
  top_destinations: ConnectionTopItem[];
  top_users: ConnectionTopItem[];
  api_available: boolean;
  received_at: string;
};

export type ProbeHistory = {
  id: string;
  node_id: string;
  site_id: string;
  outbound_tag: string;
  target_url: string;
  success: boolean;
  latency_ms: number;
  error_class: string;
  sampled_at: string;
};

export type ProbeCircuit = {
  node_id: string;
  site_id: string;
  outbound_tag: string;
  state: "closed" | "open" | "half_open" | string;
  consecutive_failures: number;
  consecutive_successes: number;
  opened_at: string | null;
  half_open_at: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_latency_ms: number;
  last_error_class: string;
  reason: string;
  updated_at: string;
};

export type ProbeOverview = { node_id: string; history: ProbeHistory[]; circuits: ProbeCircuit[] };

export type Alert = {
  id: string;
  fingerprint: string;
  category: string;
  severity: string;
  site_id: string;
  node_id: string;
  title: string;
  detail: string;
  status: "open" | "resolved" | string;
  first_seen_at: string;
  last_seen_at: string;
  resolved_at: string | null;
};

export type AccessConfig = {
  fqdn: string;
  port: number;
  protocol: "http-connect";
  https_proxy_enabled: boolean;
};

export type EmployeeAccessSite = {
  id: string;
  slug: string;
  name: string;
  proxy_auth_required: boolean;
  credential_configured: boolean;
  username: string | null;
};

export type EmployeeProxyAccess = {
  itcode: string;
  sites: EmployeeAccessSite[];
};

export type Employee = {
  itcode: string;
  auth_source: string;
  is_active: boolean;
  created_at: string;
  password_changed_at: string | null;
  last_login_at: string | null;
};

export type ProxyCredential = {
  site_id: string;
  username: string;
  active: boolean;
  rotated_at: string;
};

export type ProxyCredentialReveal = {
  site_id: ProxyCredential["site_id"];
  username: ProxyCredential["username"];
  active: ProxyCredential["active"];
  rotated_at: ProxyCredential["rotated_at"];
  password: string;
  release_id: string | null;
};

export type BackupRecord = {
  backup_id: string;
  scope: string;
  origin: "manual" | "scheduled";
  artifact_paths: string[];
  format: string;
  checksum: string;
  encrypted: boolean;
  storage_ref: string;
  status: string;
  created_by: string;
  created_at: string;
  verified_at: string | null;
  last_rehearsed_at: string | null;
  restore_task_id: string | null;
  error: string;
  size_bytes: number;
  manifest: Record<string, unknown>;
};

export type BackupCreateResponse = { backup: BackupRecord; task: Task };
export type BackupRestoreResponse = { backup: BackupRecord; task: Task };

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail || `API request failed (${status})`);
    this.name = "ApiError";
  }
}

const baseURL = (process.env.NEXT_PUBLIC_API_BASE_URL || "/backend-api").replace(
  /\/$/,
  "",
);
const tokenKey = "grouproxy.management_token";
const roleKey = "grouproxy.session_role";

export type SessionRole = AuthSession["role"];

export function managementToken() {
  if (typeof window !== "undefined") {
    return window.localStorage.getItem(tokenKey) || "";
  }
  return "";
}

export function hasManagementSession() {
  return Boolean(managementToken()) && managementSessionRole() !== "employee";
}

export function hasAuthenticatedSession() {
  return Boolean(managementToken());
}

export function managementSessionRole(): SessionRole | null {
  if (typeof window === "undefined") return null;
  const role = window.localStorage.getItem(roleKey);
  return role === "admin" || role === "employee" ? role : null;
}

export function clearManagementSession() {
  if (typeof window !== "undefined") {
    window.localStorage.removeItem(tokenKey);
    window.localStorage.removeItem(roleKey);
  }
}

export function saveManagementSession(token: string, role: SessionRole) {
  if (typeof window !== "undefined") {
    window.localStorage.setItem(tokenKey, token);
    window.localStorage.setItem(roleKey, role);
  }
}

export function loginWithPassword(itcode: string, password: string) {
  return request<AuthSession>("/api/v1/auth/login", jsonRequest("POST", { itcode, password }));
}

export function requestAuthVerificationCode(itcode: string, purpose: VerificationPurpose) {
  return request<VerificationChallenge>(
    "/api/v1/auth/verification-codes",
    jsonRequest("POST", { itcode, purpose }),
  );
}

export function registerAccount(value: {
  itcode: string;
  password: string;
  challenge_id: string;
  verification_code: string;
}) {
  return request<{ status: "ok" }>("/api/v1/auth/register", jsonRequest("POST", value));
}

export function loginWithGQuan(value: {
  itcode: string;
  challenge_id: string;
  verification_code: string;
}) {
  return request<AuthSession>("/api/v1/auth/gquan/login", jsonRequest("POST", value));
}

export function changeAccountPassword(value: {
  itcode: string;
  password: string;
  challenge_id: string;
  verification_code: string;
}) {
  return request<{ status: "ok" }>("/api/v1/auth/password/change", jsonRequest("POST", value));
}

export function logoutManagementSession() {
  return request<{ status: "ok" }>("/api/v1/auth/logout", jsonRequest("POST"));
}

function errorDetail(body: unknown): string {
  if (!body || typeof body !== "object" || !("detail" in body)) return "request_failed";
  const detail = body.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return "validation_error";
  }
  if (detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string") {
    return detail.code;
  }
  return "request_failed";
}

async function readError(response: Response) {
  try {
    const detail = errorDetail(await response.json());
    if (detail !== "request_failed") return detail;
  } catch {
    // A proxy or reverse proxy may return a non-JSON error body.
  }
  if (response.status >= 500) return "network_error";
  return response.statusText || "request_failed";
}

async function apiFetch(path: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(`${baseURL}${path}`, { ...init, cache: "no-store" });
  } catch {
    throw new ApiError(0, "network_error");
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  const token = managementToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await apiFetch(path, { ...init, headers });
  if (!response.ok) throw new ApiError(response.status, await readError(response));
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function requestText(path: string): Promise<string> {
  const token = managementToken();
  const response = await apiFetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) throw new ApiError(response.status, await readError(response));
  return response.text();
}

async function requestForm<T>(path: string, form: FormData): Promise<T> {
  const headers = new Headers();
  const token = managementToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await apiFetch(path, {
    method: "POST",
    headers,
    body: form,
  });
  if (!response.ok) throw new ApiError(response.status, await readError(response));
  return (await response.json()) as T;
}

function jsonRequest(method: "POST" | "PUT" | "PATCH" | "DELETE", body?: unknown): RequestInit {
  return {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

export function createIdempotencyKey(prefix: string) {
  const suffix = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}:${suffix}`;
}

export function getOverview() {
  return request<Overview>("/api/v1/overview");
}

export function getSites() {
  return request<Site[]>("/api/v1/sites");
}

export function updateSiteName(siteId: string, name: string) {
  return request<Site>(
    `/api/v1/sites/${encodeURIComponent(siteId)}`,
    jsonRequest("PATCH", { name }),
  );
}

export function getEmployees() {
  return request<Employee[]>("/api/v1/employees");
}

export function getEmployeeProxyCredentials(itcode: string) {
  return request<ProxyCredential[]>(
    `/api/v1/employees/${encodeURIComponent(itcode)}/proxy-credentials`,
  );
}

export function getNodes() {
  return request<Node[]>("/api/v1/nodes");
}

export function updateNodeName(nodeId: string, name: string) {
  return request<Node>(
    `/api/v1/nodes/${encodeURIComponent(nodeId)}`,
    jsonRequest("PATCH", { name }),
  );
}

export function getProxyConfigs(filters: { siteId?: string; nodeId?: string } = {}) {
  const query = new URLSearchParams();
  if (filters.siteId) query.set("site_id", filters.siteId);
  if (filters.nodeId) query.set("node_id", filters.nodeId);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<ProxyConfigSnapshot[]>(`/api/v1/proxy-configs${suffix}`);
}

export function getNodeProxyConfig(nodeId: string) {
  return request<ProxyConfigSnapshot>(
    `/api/v1/nodes/${encodeURIComponent(nodeId)}/proxy-config`,
  );
}

export function selectNodeProxy(nodeId: string, value: ProxySelectionRequest) {
  const idempotencyKey = [
    "proxy-selection",
    encodeURIComponent(nodeId),
    encodeURIComponent(value.group.trim()),
    encodeURIComponent(value.outbound.trim()),
    encodeURIComponent(String(value.expected_current_version ?? "latest")),
  ].join(":");
  return request<Release>(
    `/api/v1/nodes/${encodeURIComponent(nodeId)}/proxy-selection`,
    {
      ...jsonRequest("POST", value),
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
    },
  );
}

export function setSiteShutdown(siteId: string, shutdown: boolean) {
  return request<Site>(`/api/v1/sites/${siteId}/shutdown`, jsonRequest("POST", { shutdown }));
}

export function setSiteProxyAuth(siteId: string, required: boolean) {
  return request<Site>(`/api/v1/sites/${siteId}/proxy-auth`, jsonRequest("PUT", { required }));
}

export function getSiteCIDRs(siteId: string) {
  return request<CIDREntry[]>(`/api/v1/sites/${siteId}/cidrs`);
}

export function addCIDR(siteId: string, value: Omit<CIDREntry, "id" | "site_id">) {
  return request<CIDREntry>(`/api/v1/sites/${siteId}/cidrs`, jsonRequest("POST", value));
}

export function deleteCIDR(siteId: string, cidrId: string) {
  return request<void>(`/api/v1/sites/${siteId}/cidrs/${cidrId}`, jsonRequest("DELETE"));
}

export function previewCIDR(siteId: string, sourceIP: string) {
  return request<CIDRPreview>(
    "/api/v1/cidrs/preview",
    jsonRequest("POST", { site_id: siteId, source_ip: sourceIP }),
  );
}

export function getExceptions() {
  return request<TravelException[]>("/api/v1/exceptions");
}

export function createException(value: Omit<TravelException, "id" | "created_at">) {
  return request<TravelException>("/api/v1/exceptions", jsonRequest("POST", value));
}

export function deleteException(exceptionId: string) {
  return request<void>(`/api/v1/exceptions/${exceptionId}`, jsonRequest("DELETE"));
}

export function getCrossSiteAllows() {
  return request<CrossSiteAllow[]>("/api/v1/cross-site-allows");
}

export function saveCrossSiteAllow(
  value: Omit<CrossSiteAllow, "id" | "updated_at">,
) {
  return request<CrossSiteAllow>("/api/v1/cross-site-allows", jsonRequest("PUT", value));
}

export function getBlacklist() {
  return request<DestinationBlacklist[]>("/api/v1/blacklist");
}

export function createBlacklist(
  value: Omit<DestinationBlacklist, "id" | "created_at">,
) {
  return request<DestinationBlacklist>("/api/v1/blacklist", jsonRequest("POST", value));
}

export function deleteBlacklist(entryId: string) {
  return request<void>(`/api/v1/blacklist/${entryId}`, jsonRequest("DELETE"));
}

export function getSubscriptions() {
  return request<SubscriptionCatalog>("/api/v1/subscriptions");
}

export function createSubscriptionSource(value: {
  name: string;
  url: string;
  fetch_interval_sec?: number;
  max_body_bytes?: number;
  redirect_limit?: number;
}) {
  return request<SubscriptionRefreshResponse>("/api/v1/subscriptions", {
    ...jsonRequest("POST", value),
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": createIdempotencyKey("subscription.create"),
    },
  });
}

export function uploadSubscription(name: string, file: File) {
  const form = new FormData();
  form.set("name", name);
  form.set("file", file);
  return requestForm<SubscriptionUploadResponse>("/api/v1/subscriptions/upload", form);
}

export function refreshSubscription(sourceId: string) {
  return request<SubscriptionRefreshResponse>(`/api/v1/subscriptions/${sourceId}/refresh`, {
    ...jsonRequest("POST", {}),
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": createIdempotencyKey(`subscription.refresh:${sourceId}`),
    },
  });
}

export function publishSubscriptionVersion(
  sourceId: string,
  versionId: string,
  siteIds: string[],
) {
  return request<SubscriptionPublishResponse>(
    `/api/v1/subscriptions/${sourceId}/versions/${versionId}/publish`,
    {
      ...jsonRequest("POST", { site_ids: siteIds, note: "Published from operations console" }),
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": createIdempotencyKey(`subscription.publish:${versionId}`),
      },
    },
  );
}

export function rollbackSiteSubscription(siteId: string) {
  return request<SubscriptionPublishResponse>(
    `/api/v1/subscriptions/sites/${siteId}/rollback`,
    {
      ...jsonRequest("POST", {}),
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": createIdempotencyKey(`subscription.rollback:${siteId}`),
      },
    },
  );
}

export function getDrafts() {
  return request<Draft[]>("/api/v1/config/drafts");
}

export function createDraft(value: {
  site_id: string;
  node_ids?: string[];
  diff: Record<string, unknown>;
  note?: string;
}) {
  return request<Draft>("/api/v1/config/drafts", jsonRequest("POST", value));
}

export function getReleases(filters: { siteId?: string; status?: string; since?: string; until?: string } = {}) {
  const query = new URLSearchParams();
  if (filters.siteId) query.set("site_id", filters.siteId);
  if (filters.status) query.set("status", filters.status);
  if (filters.since) query.set("since", filters.since);
  if (filters.until) query.set("until", filters.until);
  return request<Release[]>(`/api/v1/config/releases${query.size ? `?${query.toString()}` : ""}`);
}

export function getRelease(releaseId: string) {
  return request<Release>(`/api/v1/config/releases/${releaseId}`);
}

export function getReleaseAcks(releaseId: string) {
  return request<AgentAck[]>(`/api/v1/config/releases/${releaseId}/acks`);
}

export function publishRelease(value: {
  draft_id: string;
  site_id: string;
  node_ids?: string[];
  expected_current_version?: number | null;
  note?: string;
}) {
  const idempotencyKey = createIdempotencyKey(`config.publish:${value.draft_id}`);
  return request<Release>("/api/v1/config/releases", {
    ...jsonRequest("POST", value),
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
  });
}

export function getTasks(filters: { status?: string; taskType?: string; since?: string; until?: string } = {}) {
  const query = new URLSearchParams();
  if (filters.status) query.set("status", filters.status);
  if (filters.taskType) query.set("task_type", filters.taskType);
  if (filters.since) query.set("since", filters.since);
  if (filters.until) query.set("until", filters.until);
  return request<Task[]>(`/api/v1/tasks${query.size ? `?${query.toString()}` : ""}`);
}

export function getTask(taskId: string) {
  return request<Task>(`/api/v1/tasks/${encodeURIComponent(taskId)}`);
}

export function getLogs(filters: { siteId?: string; nodeId?: string; action?: "allow" | "deny"; since?: string; until?: string; search?: string } = {}) {
  const query = new URLSearchParams();
  if (filters.siteId) query.set("site_id", filters.siteId);
  if (filters.nodeId) query.set("node_id", filters.nodeId);
  if (filters.action) query.set("action", filters.action);
  if (filters.since) query.set("since", filters.since);
  if (filters.until) query.set("until", filters.until);
  if (filters.search) query.set("search", filters.search);
  return request<AccessLog[]>(`/api/v1/logs${query.size ? `?${query.toString()}` : ""}`);
}

export function getConnections(filters: { siteId?: string; nodeId?: string; since?: string; until?: string } = {}) {
  const query = new URLSearchParams();
  if (filters.siteId) query.set("site_id", filters.siteId);
  if (filters.nodeId) query.set("node_id", filters.nodeId);
  if (filters.since) query.set("since", filters.since);
  if (filters.until) query.set("until", filters.until);
  return request<ConnectionSnapshot[]>(`/api/v1/connections${query.size ? `?${query.toString()}` : ""}`);
}

export function getNodeProbes(nodeId: string) {
  return request<ProbeOverview>(`/api/v1/nodes/${encodeURIComponent(nodeId)}/probes`);
}

export function createNodeProbe(nodeId: string, value: { target_url?: string; outbound_tags?: string[] } = {}) {
  return request<Task>(`/api/v1/nodes/${encodeURIComponent(nodeId)}/probes`, {
    ...jsonRequest("POST", value),
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": createIdempotencyKey(`node.probe:${nodeId}`),
    },
  });
}

export function getAlerts(status?: "open" | "resolved") {
  const query = status ? `?status_filter=${status}` : "";
  return request<Alert[]>(`/api/v1/alerts${query}`);
}

export function cancelTask(taskId: string) {
  return request<Task>(`/api/v1/tasks/${taskId}/cancel`, jsonRequest("POST", {}));
}

export function getAudit(filters: { since?: string; until?: string; action?: string; actor?: string } = {}) {
  const query = new URLSearchParams();
  if (filters.since) query.set("since", filters.since);
  if (filters.until) query.set("until", filters.until);
  if (filters.action) query.set("action", filters.action);
  if (filters.actor) query.set("actor", filters.actor);
  return request<AuditEvent[]>(`/api/v1/audit${query.size ? `?${query.toString()}` : ""}`);
}

export function verifyAudit() {
  return request<{ valid: boolean; error: string; event_count: number }>("/api/v1/audit/verify");
}

export function getAuditExport(exportFormat: "json" | "ndjson" = "json") {
  return requestText(`/api/v1/audit/export?export_format=${exportFormat}`);
}

export function getBackups() {
  return request<BackupRecord[]>("/api/v1/backups");
}

export function createBackup() {
  return request<BackupCreateResponse>("/api/v1/backups", {
    ...jsonRequest("POST", { scope: "control_plane" }),
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": createIdempotencyKey("backup.create"),
    },
  });
}

export function restoreBackup(backupId: string, confirm = false) {
  return request<BackupRestoreResponse>(`/api/v1/backups/${encodeURIComponent(backupId)}/restore`, {
    ...jsonRequest("POST", { confirm }),
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": createIdempotencyKey(`backup.restore:${backupId}:${confirm ? "apply" : "rehearsal"}`),
    },
  });
}

export function getLinuxSetupScript() {
  return requestText("/api/v1/access/linux-setup.sh");
}

export function getAccessConfig() {
  return request<AccessConfig>("/api/v1/access/config");
}

export function getEmployeeProxyAccess() {
  return request<EmployeeProxyAccess>("/api/v1/access/proxy-credentials");
}

export function rotateOwnProxyCredential(siteId: string) {
  return request<ProxyCredentialReveal>(
    `/api/v1/access/proxy-credentials/${encodeURIComponent(siteId)}/rotate`,
    jsonRequest("POST", {}),
  );
}

export function rotateEmployeeProxyCredential(siteId: string, itcode: string) {
  return request<ProxyCredentialReveal>(
    `/api/v1/sites/${encodeURIComponent(siteId)}/proxy-credentials/${encodeURIComponent(itcode)}/rotate`,
    jsonRequest("POST", {}),
  );
}

export function getProxyPAC() {
  return requestText("/api/v1/access/proxy.pac");
}
