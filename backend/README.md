# Grouproxy Backend

The FastAPI application exposes separate management and agent surfaces:

- `/api/v1/*` is used by the operations dashboard and accepts an opaque
  browser session created by `/api/v1/auth/*`. The static management Bearer
  token remains available for the existing local node-validation scripts.
- `/agent/v1/*` is used by monitors and requires a node Bearer token. Desired
  Bundles are additionally protected by an HMAC.

For the supported test environment, configure the codedev MongoDB URI and run
the repository-level script. It creates local runtime credentials but does not
start MongoDB:

```bash
GROUPROXY_TEST_MONGODB_URL='mongodb://<user>:<password>@<host>:<port>/?authSource=admin' \
  GROUPROXY_TESTENV_RESET=1 ./scripts/testenv-up.sh
```

Outside that script, `GROUPROXY_BUNDLE_HMAC_SECRET`,
`GROUPROXY_ADMIN_PASSWORD`, and `GROUPROXY_MANAGEMENT_TOKEN` are mandatory.
The HTTP agent channel also requires an explicit
`GROUPROXY_ALLOW_INSECURE_AGENT_HTTP=true` opt-in.

For One Login verification delivery, configure
`GROUPROXY_GQUAN_APP_TOKEN` with an approved APP Bearer token. The default API
base is `https://one.1oa.com.cn/springboard/api/v1`; this is an outbound call
to One Login, not a TLS listener in Grouproxy. The default test profile also
uses the real APP API, accepting its token only as the runtime
`GROUPROXY_TEST_GQUAN_APP_TOKEN` input. Do not place either token in the
repository, audit records, browser configuration, or test fixture.

Phase 0/1 covers sites, nodes, source blacklist policy, drafts, releases,
ACKs, tasks, and the audit hash chain. Phase 2 adds
`subscription_source`, immutable `subscription_version`, and per-site
selection records plus these management operations:

- list/register/refresh HTTP sources, upload a source file, and import one
  VLESS or standard Base64 VMess URI as an immutable single-node source;
- publish a parsed version to selected sites and create a normal release;
- roll back a site's selected version through the same release and ACK path;
- serve a selected large blob only to the node whose Desired Bundle references
  its hash.

HTTP subscription URLs may be `http://` or `https://`, have no embedded
credentials, and are never returned by the API. HTTPS fetches use
`verify=False` because internal upstreams often present untrusted
certificates. Fetching validates all DNS results and every redirect against
SSRF targets. Full sing-box client documents are accepted: grouping
outbounds such as `selector`, `urltest`, `direct`, and `dns` are ignored and
only endpoint outbounds are counted. Clash Meta client profiles that only
declare `proxy-providers` are rejected; use a node list (`proxies`) or a
sing-box outbound document. Uploaded and direct single-node sources are
intentionally immutable and are not scheduled for refresh. The single-node
importer normalizes raw `vless://` and `vmess://` values into one sing-box
outbound. A Reality URI must include the endpoint UUID, host and port, SNI,
Reality public key, short ID, and any required Vision flow. A VMess URI must
carry its Base64 JSON payload with the UUID, server, port, and transport/TLS
fields required by that endpoint. Refresh work uses an active-task partial
unique index, lease recovery, backoff, cancellation, and dead-letter state.
Publish and rollback derive stable per-site task keys from `Idempotency-Key`,
so retries return the existing releases without overwriting rollback history.

Proxy access is not an application credential flow. Desired Bundles always
declare HTTP CONNECT port `1080`; source access is allow-all by default because
proxy domains resolve through local DNS. Explicit `SourceBlacklist` rules may
deny a source IP, network, or domain globally or for one site. New bundles emit
only applicable enabled entries in `source_blacklist`; retired allowlist,
exception, cross-site, and destination-policy state is removed during startup
migration and is never used to build a bundle. The dashboard is expected to be
served directly by Next.js on port `80`, whose `/api/*` rewrite reaches this
backend without an external reverse proxy.

The public access endpoints expose immutable workstation assets selected by
`GROUPROXY_ENVIRONMENT`: `GET /api/v1/access/linux-setup.sh`,
`GET /api/v1/access/windows-setup.ps1`, `GET /api/v1/access/proxy.pac`, and
`GET /api/v1/access/config`. The `test` profile uses the test proxy domain and
dashboard-relative macOS `.shortcut` download path; production is selected for
all other environment values. The Linux and Windows script files are stored in
`deploy/`, run without parameters, and toggle the current user's proxy state.
They are intentionally not rendered from request data.

Authentication uses `itcode` as the primary account identity. Registration,
password changes, and passwordless GQuan login all consume a single-use
verification challenge. Challenges are rate-limited, HMAC-digested, and
short-lived; browser access tokens are opaque server-side sessions, last 30
days by default (`GROUPROXY_AUTH_SESSION_TTL_MINUTES=43200`), and become invalid
when the password changes. Protected endpoints distinguish expired, revoked,
invalid, and inactive-account sessions in their `401` response details.

Proxy configuration is an operator-facing, read-only projection of each
node's loopback Clash `/proxies` API. Monitors post it to
`/agent/v1/proxy-config`; the control plane stores one bounded latest snapshot
per node and exposes it through `GET /api/v1/proxy-configs` (also available as
`/api/v1/proxies`) and `GET /api/v1/nodes/{id}/proxy-config`. Server addresses,
subscription payloads, and endpoint credentials are discarded before storage.
When a node's local API is temporarily unavailable, the latest known group
projection is retained while the snapshot is marked unavailable.

Administrators can change only a node's display label with
`PATCH /api/v1/nodes/{id}` and `{ "name": "..." }`. The immutable `agent_id`,
node token, and site binding are never changed; successful renames are written
to the audit chain and are reflected on both the node inventory and proxy
configuration pages.
