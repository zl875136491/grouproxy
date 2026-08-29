# Grouproxy

Grouproxy is a regional proxy control plane. The backend computes signed
Desired Bundles, while each monitor owns its local sing-box process and
proxy-port firewall policy. User traffic never traverses the control plane.

## Phase 0/1/2 Quickstart

Requirements: Python 3.12, Go 1.22+, Node.js, `nft`, MongoDB binaries, and
the checked-in Linux amd64 sing-box binary.

```bash
./scripts/testenv-up.sh
./scripts/verify-phase1.sh
./scripts/verify-phase2.sh
```

The script starts an isolated MongoDB on `127.0.0.1:27018`, the backend on
`127.0.0.1:8000`, the operations console on `http://127.0.0.1:3000`, and two
local agent simulations:

- `codedev` monitor + sing-box on `127.0.0.1:18080`, Clash API on `127.0.0.1:19090`
- `nuc` monitor + sing-box on `127.0.0.1:18081`, Clash API on `127.0.0.1:19091`

The test database, generated node tokens, state, and logs remain below the
ignored `testenv/` directory. Stop the environment without removing evidence:

```bash
./scripts/testenv-down.sh
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
| `backend/` | FastAPI control plane, MongoDB documents, refresh worker |
| `frontend/` | Next.js operations dashboard |
| `monitor/` | Go monitor, local routing data, runtime, and nftables |
| `singbox/` | Pinned Linux amd64 sing-box executable |
| `deploy/` | systemd units, employee setup, node installer |
| `scripts/` | local development and Phase 0/1/2 validation |
