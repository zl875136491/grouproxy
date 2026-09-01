# Grouproxy

Grouproxy is a regional proxy control plane. The backend computes signed
Desired Bundles, while each monitor owns its local sing-box process and
proxy-port firewall policy. User traffic never traverses the control plane.

## Phase 0-4 Quickstart

Requirements: Python 3.12, Go 1.22+, Node.js, `nft`, access to the codedev
MongoDB test database, and the checked-in Linux amd64 sing-box binary.

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

The script validates connectivity and authentication against the explicitly
configured codedev MongoDB URI before it starts any local process. It does not
start, reset, or shut down a local MongoDB process. It starts the backend on
`127.0.0.1:8000`, the operations console on `http://127.0.0.1:3000`, and two
local agent simulations:

- `codedev` monitor + sing-box on `127.0.0.1:18080`, Clash API on `127.0.0.1:19090`
- `nuc` monitor + sing-box on `127.0.0.1:18081`, Clash API on `127.0.0.1:19091`
- `/proxies` presents each region's read-only strategy groups and outbound
  health projection; administrators rename node display labels from `/nodes`.

Generated test credentials, state, and logs remain below the ignored
`testenv/` directory. Stop the local processes without removing evidence:

```bash
./scripts/testenv-down.sh
```

The default test profile delivers verification codes through the real One
Login GQuan APP API. Its APP Token is required at process start and is not
written to `testenv/backend.env`, logs, or Git. Use the login page to complete
a real verification flow. The deterministic authentication regression remains
available only in an explicitly isolated stub profile:

```bash
GROUPROXY_TEST_GQUAN_DELIVERY_MODE=stub GROUPROXY_TESTENV_RESET=1 ./scripts/testenv-up.sh
./scripts/verify-auth.sh
```

When changing GQuan mode for an existing shared-database test runtime, stop
the local processes and reconfigure only the non-sensitive mode value. This
preserves the one-time node tokens that the shared database cannot reveal:

```bash
./scripts/testenv-down.sh
GROUPROXY_TEST_GQUAN_APP_TOKEN='sat_<approved-app-token>' \
  GROUPROXY_TESTENV_RECONFIGURE_GQUAN=1 ./scripts/testenv-up.sh
```

Build the committed monitor artifact and checksum with:

```bash
(cd monitor && make dist)
```

## Phase 2 Subscription Delivery

The operations console at `/subscriptions` registers HTTP sources or uploads
Clash YAML, SIP008, and sing-box outbound JSON. Every successful fetch or
upload is stored as an immutable SHA-256 version. Invalid content remains
visible for diagnosis but cannot be published.

- Source fetches validate every DNS result and redirect target, reject local
  and non-global addresses, cap response size, and never return source URLs or
  raw content to the management UI.
- One active refresh task is allowed per source. The MongoDB worker uses a
  lease, heartbeat, retry backoff, cancellation, and dead-letter state.
- Publish and rollback requests accept `Idempotency-Key`; a duplicate request
  returns the same per-site releases instead of creating another deployment.
- A release selects one immutable version per site. Each monitor validates the
  blob hash, renders its own outbounds, runs `sing-box check`, and preserves
  the resolved last-good configuration on failure.
- The monitor embeds pinned local `geoip-cn` and `geosite-cn` sing-box
  rule-sets. Destination deny rules run first, Chinese domains and IPs route
  direct, and other traffic uses the selected subscription. Nodes never fetch
  routing data or subscription-provider URLs themselves.

## Phase 3 Observability

`scripts/verify-phase3.sh` performs only local control-plane checks. It verifies
canonical telemetry fields, alert and audit APIs, node probe summaries, PAC,
and the HTTP-only Linux setup script. It does not create a probe, backup, or
external proxy request.

The frontend also provides two focused i18n checks:

```bash
(cd frontend && npm run test:i18n)
```

The browser check needs a local Chrome binary and the generated test admin
credentials in the current process environment. It loads cached connection
telemetry, switches Chinese, English, and Spanish, and asserts that no API
request occurs during either locale switch.

## Account Authentication

The `/login` screen uses an `itcode` as the account identity. Password login,
registration, password changes, and GQuan code login are backed by opaque,
time-limited server sessions. New passwords use Argon2; pre-existing seeded
password hashes are upgraded after a successful password login.

- Registration, password changes, and GQuan login require a six-digit,
  single-use verification code delivered with the documented One Login GQuan
  APP Bearer token.
- Verification codes are HMAC-digested, expire after a short interval, are
  rate-limited per itcode and purpose, and lock after repeated failures. Raw
  codes, APP tokens, and GQuan response bodies are not logged or audited.
- Set `GROUPROXY_GQUAN_APP_TOKEN` in the backend environment. The default
  test profile also uses a runtime-only APP Token and sends real GQuan
  messages only after an operator requests a code from the login page.

## Phase 4 HTTP Basic Credentials

Phase 4 keeps the employee data path on HTTP CONNECT while adding optional
per-site HTTP Basic authentication. An employee receives one credential per
site from `/access`; creating or rotating one reveals the generated password
exactly once. The backend stores an Argon2 verifier and a random credential
identifier, never the clear-text password. A stable
`GROUPROXY_PROXY_CREDENTIAL_SECRET` derives the password again only when the
signed Desired Bundle is built, so it must be retained with other control-plane
secrets for backups and restores.

- An administrator enables site authentication only after at least one active
  credential exists. Credential changes for an enabled site create a normal,
  auditable release; nodes continue to accept their last-good user list during
  a control-plane outage.
- Administrators manage registered employee identities and per-site credential
  metadata at `/employees`. `GET /api/v1/employees` and
  `GET /api/v1/employees/{itcode}/proxy-credentials` intentionally return
  metadata only; issuing or rotating a password remains a one-time reveal.
- Credential responses use `Cache-Control: no-store`; passwords are omitted
  from audit records, logs, ordinary APIs, and MongoDB documents.
- Monitor `0.3.0` validates the signed `proxy_auth` object and renders the
  users into the local sing-box HTTP inbound. It also translates Clash Trojan
  SNI and certificate-validation fields to sing-box TLS settings for the
  subscription's *outbound* connection.
- `./scripts/verify-phase4.sh` exercises the local Basic-authentication flow.
  It does not make an external request by default. Set
  `GROUPROXY_VERIFY_PROXY_EXTERNAL=1` only for the single approved
  `HEAD https://www.google.com/ncr` check.

## Deployment Boundary

`codedev` is the only control plane. Both `codedev` and `nuc` are agent nodes
running monitor and sing-box. Remote installation is explicit and
parameterized:

```bash
NUC_SSH_USER=operator NUC_SSH_KEY=/path/to/key \
  ./deploy/install-node.sh 10.32.12.110
```

The supported path is deliberately HTTP-only: employee access is HTTP CONNECT
on port `80`, and the test control channel is HTTP with Bearer authentication
and bundle HMAC. This repository does not configure TLS, HTTPS proxy
listeners, certificate material, client certificates, mTLS, port `443`, or
CI/CD automation.

## Layout

| Directory | Responsibility |
| --- | --- |
| `backend/` | FastAPI control plane, MongoDB documents, auth and refresh workers |
| `frontend/` | Next.js operations dashboard |
| `monitor/` | Go monitor, local routing data, runtime, and nftables |
| `singbox/` | Pinned Linux amd64 sing-box executable |
| `deploy/` | systemd units, employee setup, node installer |
| `scripts/` | local development and Phase 0/1/2/auth validation |
