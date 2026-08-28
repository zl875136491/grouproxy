# Grouproxy deployment

Phase 0/1 uses one control plane on `codedev` and two agent-capable nodes:

- `codedev`: backend, frontend, MongoDB, monitor and sing-box.
- `nuc`: monitor and sing-box only.

The test environment uses HTTP between monitor and backend with a Bearer token
and bundle HMAC. This is deliberate for the current certificate constraint; it
is not a production security posture. Production must use the HTTPS transport
and a rotated node credential before exposing the control plane beyond the
isolated test network.

The employee path is HTTP CONNECT on `:80`. No `:443` listener, TLS material,
CA installation, or HTTPS proxy is enabled in this phase. The script
`linux-setup-proxy.sh` configures `http_proxy` and `https_proxy` to the HTTP
proxy URL, which still carries HTTPS destinations through CONNECT.

## Local validation

From the repository root:

```bash
./scripts/testenv-up.sh
./scripts/verify-phase1.sh
./scripts/testenv-down.sh
```

Runtime state, tokens, logs, and the temporary MongoDB data directory are
created under `testenv/` and are ignored by Git.

## Remote node installation

`install-node.sh` requires an explicit SSH user and optional key. It does not
guess credentials and does not use Jenkins credentials:

```bash
NUC_SSH_USER=operator NUC_SSH_KEY=/path/to/key \
  ./deploy/install-node.sh 10.32.12.110
```

Use `DRY_RUN=1` to inspect the commands when access to the node is not yet
available. The script copies only the monitor/sing-box artifacts and systemd
units; the control plane is never installed remotely. It enables only
`grouproxy-monitor.service`: monitor owns the sing-box child lifecycle, so
enabling `sing-box.service` as well would create a second process competing
for the proxy listener.
