# Grouproxy

Grouproxy is a regional proxy control plane. The backend computes signed
Desired Bundles, while each monitor owns its local sing-box process and
proxy-port firewall policy. User traffic never traverses the control plane.

## Network Contract

The data plane and dashboard are separate direct listeners. No forwarding
layer is part of this deployment.

- The proxy domain serves HTTP CONNECT directly on TCP `1080`.
- The dashboard is served directly by Next.js on TCP `80`.
- The dashboard's `/api/*` requests are rewritten by Next.js to the backend;
  the backend can remain on a private listener such as `127.0.0.1:8000`.
- The dashboard forwards `/healthz` and `/readyz` to that backend, so
  `curl http://<dashboard-domain>/healthz` exposes the running API version.
- HTTP Basic proxy authentication is intentionally absent. Source CIDR policy
  on each site is the proxy access boundary.

The monitor validates signed bundles with `listen.http_port: 1080` and renders
the public sing-box HTTP inbound on that port. The same-host two-node test
harness has one explicit public test exception for its second node on `:18081`;
deployed nodes remain on `:1080`.

The `/access` dashboard page is the workstation runbook. It selects a pair of
checked-in, pre-generated assets from `GROUPROXY_ENVIRONMENT`: `test` serves
`test-proxy.1oa.com.cn`, while every other value serves production
`proxy.1oa.com.cn`. Authenticated downloads are available at
`/api/v1/access/linux-setup.sh` and `/api/v1/access/windows-setup.ps1`; the
configuration response also returns the matching macOS iCloud Shortcut URL.
The Windows script configures the current user's WinINET and environment
proxy, runs the supplied connectivity checks, and accepts `-Disable` to
restore its backup. No downloaded asset installs proxy credentials or a CA.

The production dashboard is a Next.js standalone service. Its deployable
systemd unit and replacement steps for retired Grouproxy NGINX files are in
[`deploy/README.md`](deploy/README.md).

## Quickstart

Requirements: Python 3.12, Go 1.22+, Node.js, `nft`, access to the codedev
MongoDB test database, and the checked-in Linux amd64 sing-box binary. Running
the production dashboard directly on `:80` requires the service account to
have permission to bind that port, for example `CAP_NET_BIND_SERVICE`.

```bash
export GROUPROXY_TEST_MONGODB_URL='mongodb://<user>:<password>@<codedev-host>:<port>/?authSource=admin'
export GROUPROXY_TEST_MONGODB_DATABASE='grouproxy_test'
export GROUPROXY_TEST_GQUAN_APP_TOKEN='sat_<approved-app-token>'
GROUPROXY_TESTENV_RESET=1 ./scripts/testenv-up.sh
./scripts/verify-phase1.sh
./scripts/verify-phase2.sh
./scripts/verify-phase3.sh
./scripts/verify-phase4.sh
```

The script validates the configured MongoDB URI before it starts any local
process. It does not create, reset, or stop MongoDB. All client-facing test
listeners bind to the network; the backend and Clash APIs remain loopback-only.
The same-host simulation uses a distinct public port for its second node:

- dashboard on `0.0.0.0:3000`, with `/api/*` sent directly to the private backend
- `codedev` monitor + sing-box on `0.0.0.0:1080`, Clash API on `127.0.0.1:19090`
- `nuc` monitor + sing-box on `0.0.0.0:18081`, Clash API on `127.0.0.1:19091`

The test host must be permitted to bind TCP `1080`. Point a temporary hosts
entry or the eventual DNS record for `test-proxy.1oa.com.cn` at the IP of this
host, then use these client-facing endpoints:

- console: `http://test-proxy.1oa.com.cn:3000/`
- codedev proxy: `http://test-proxy.1oa.com.cn:1080`
- nuc proxy: `http://test-proxy.1oa.com.cn:18081`

`GROUPROXY_TEST_CLIENT_CIDR` defaults to `10.32.12.0/24` and is included in
both test sites so the workstation can exercise both public listeners. Set it
to a narrower CIDR when the test client address is known. The `:18081` endpoint
exists only because both simulations share one host; an actual `nuc` host uses
its own address on `:1080`.

Generated test state and logs remain below the ignored `testenv/` directory.
Stop the local processes without removing evidence:

```bash
./scripts/testenv-down.sh
```

The default test profile delivers verification codes through the real One
Login GQuan APP API. Its APP token is supplied only at process start and is
not written to `testenv/backend.env`, logs, or Git. The deterministic auth
regression is available only in an explicitly isolated stub profile:

```bash
GROUPROXY_TEST_GQUAN_DELIVERY_MODE=stub GROUPROXY_TESTENV_RESET=1 ./scripts/testenv-up.sh
./scripts/verify-auth.sh
```

Build the committed monitor artifact and checksum with:

```bash
(cd monitor && make dist)
```

## Subscription Delivery

The operations console at `/subscriptions` supports three source types:

- an HTTP subscription site;
- an immutable uploaded Clash YAML, SIP008, or sing-box outbound file;
- one direct VLESS or VMess URI, including VLESS Reality Vision and the
  standard `vmess://<base64-json>` format.

Use **Add source** and select either **Subscription site connection** or
**Single node**. A normal, unescaped VLESS URI contains all information needed
for the supplied Reality example: UUID, server and port, SNI, fingerprint,
Reality public key, short ID, and Vision flow. A VMess share link must contain
the standard Base64-encoded JSON with its UUID, server, port, security mode,
and transport settings. Paste the raw `vless://...` or `vmess://...` value,
without Markdown escape backslashes. The control plane decodes and validates
the URI, normalizes it into one immutable sing-box outbound, and publishes it
through the same version, release, and node ACK flow as a multi-node
subscription. No additional subscription URL or account metadata is required;
the endpoint credentials and handshake parameters inside the link are the
single node definition.

HTTP source fetches validate every DNS result and redirect target, reject local
and non-global addresses, cap response size, and never return source URLs or
raw content to the management UI. One active refresh task is allowed per HTTP
source. Uploaded and single-node sources are immutable and are not refreshed.

Each release selects one immutable version per site. Each monitor validates the
blob hash, renders its own outbounds, runs `sing-box check`, and preserves the
resolved last-good configuration on failure. Nodes never fetch routing data or
subscription-provider URLs themselves.

## Observability And Accounts

`scripts/verify-phase3.sh` verifies local telemetry fields, alert and audit
APIs, node probe summaries, PAC, environment-specific Linux and Windows setup
assets, and the access configuration. The frontend also provides an i18n check:

```bash
(cd frontend && npm run test:i18n)
```

The `/login` screen uses an `itcode` as the account identity. Password login,
registration, password changes, and GQuan code login use opaque server
sessions with a 30-day lifetime by default
(`GROUPROXY_AUTH_SESSION_TTL_MINUTES=43200`). Expired, revoked, invalid, and
inactive-account sessions return distinct authentication errors so the console
can clear stale credentials and ask the operator to sign in again. Verification
codes are HMAC-digested, short-lived, and rate-limited. This management
authentication is separate from the proxy data plane and does not create proxy
credentials.

`scripts/verify-phase4.sh` creates an encrypted backup and runs a
non-destructive restore rehearsal.

## Deployment Boundary

`codedev` is the control plane. `codedev` and `nuc` can both be agent nodes
running monitor and sing-box. Remote installation is explicit and
parameterized:

```bash
NUC_SSH_USER=operator NUC_SSH_KEY=/path/to/key \
  ./deploy/install-node.sh 10.32.12.110
```

The supported employee path is HTTP CONNECT on port `1080`. This repository
does not configure TLS, HTTPS proxy listeners, certificate material, client
certificates, mTLS, port `443`, or CI/CD automation.

## Layout

| Directory | Responsibility |
| --- | --- |
| `backend/` | FastAPI control plane, MongoDB documents, auth and refresh workers |
| `frontend/` | Next.js operations dashboard served directly on `:80` |
| `monitor/` | Go monitor, local routing data, runtime, and nftables |
| `singbox/` | Pinned Linux amd64 sing-box executable |
| `deploy/` | systemd units, employee setup, node installer |
| `scripts/` | Local development and validation scripts |
