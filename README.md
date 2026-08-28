# Grouproxy

Grouproxy is a regional proxy control plane. The backend computes signed
Desired Bundles, while each monitor owns the local sing-box process and the
proxy-port firewall policy. User traffic never traverses the control plane.

## Phase 0/1 quickstart

Requirements: Python 3.12, Go 1.22+, Node.js, `nft`, MongoDB binaries, and the
checked-in Linux amd64 sing-box binary.

```bash
./scripts/testenv-up.sh
./scripts/verify-phase1.sh
```

The script starts an isolated, unauthenticated MongoDB on `127.0.0.1:27018`,
the backend on `127.0.0.1:8000`, the Next.js console on
`http://127.0.0.1:3000`, and two local agent simulations:

- `codedev` monitor + sing-box on `127.0.0.1:18080` with Clash API on `127.0.0.1:19090`
- `nuc` monitor + sing-box on `127.0.0.1:18081` with Clash API on `127.0.0.1:19091`

The test database, generated node tokens, state, and logs stay under the
ignored `testenv/` directory. Stop processes without removing evidence with:

```bash
./scripts/testenv-down.sh
```

For a standalone monitor build and checksum:

```bash
(cd monitor && make dist)
```

## Current boundary

The Phase 0/1 employee path is HTTP CONNECT on `:80`; the monitor/backend
test channel is HTTP plus Bearer authentication and bundle HMAC. No `:443`
listener, TLS/CA material, HTTPS proxy, Jenkins, or other CI/CD integration is
enabled in this change. Production must move the control channel to HTTPS and
rotate all test credentials before use outside an isolated network.

The control plane is intended to run on `codedev`. Both `codedev` and `nuc`
run monitor and sing-box. Remote installation is explicit and parameterized:

```bash
NUC_SSH_USER=operator NUC_SSH_KEY=/path/to/key \
  ./deploy/install-node.sh 10.32.12.110
```

## Layout

| Directory | Responsibility |
| --- | --- |
| `backend/` | FastAPI control plane and MongoDB documents |
| `frontend/` | Next.js App Router operations console |
| `monitor/` | Go monitor, signed bundle validation, runtime and nftables |
| `singbox/` | Pinned Linux amd64 sing-box executable |
| `deploy/` | systemd units, employee setup, node installer |
| `scripts/` | local development and Phase 0/1 validation |
