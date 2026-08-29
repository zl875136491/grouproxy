# Grouproxy Deployment

One control plane runs on `codedev`; both `codedev` and `nuc` are agent nodes
that run monitor and sing-box. The test environment uses HTTP between monitor
and backend with a node Bearer token and bundle HMAC.

The employee path is HTTP CONNECT on port `80`. This phase has no HTTPS proxy
listener, certificate material, client certificate handling, mTLS, or port
`443` configuration. `linux-setup-proxy.sh` sets `http_proxy` and
`https_proxy` to the HTTP proxy URL so encrypted destinations remain inside
the CONNECT tunnel.

## Local Validation

From the repository root:

```bash
./scripts/testenv-up.sh
./scripts/verify-phase1.sh
./scripts/verify-phase2.sh
./scripts/testenv-down.sh
```

Runtime state, tokens, logs, and temporary MongoDB data are created under
`testenv/` and ignored by Git.

## Remote Node Installation

`install-node.sh` requires an explicit SSH user and optional key:

```bash
NUC_SSH_USER=operator NUC_SSH_KEY=/path/to/key \
  ./deploy/install-node.sh 10.32.12.110
```

Use `DRY_RUN=1` to inspect the node commands. The script copies only monitor,
sing-box, and systemd artifacts; the control plane is never installed remotely.
It enables only `grouproxy-monitor.service` because monitor owns the sing-box
child lifecycle.
