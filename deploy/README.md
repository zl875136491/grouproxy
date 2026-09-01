# Grouproxy Deployment

One control plane runs on `codedev`; both `codedev` and `nuc` are agent nodes
that run monitor and sing-box. The test environment uses HTTP between monitor
and backend with a node Bearer token and bundle HMAC.

The employee path is HTTP CONNECT on port `80`. This phase has no HTTPS proxy
listener, certificate material, client certificate handling, mTLS, or port
`443` configuration. `linux-setup-proxy.sh` persists the shell proxy variables
for a user, updates GNOME/KDE when their settings tools are available, and can
remove only its own changes with `--uninstall`. `--system` writes only system
shell/environment defaults and requires root. All HTTPS destination traffic
remains inside the HTTP CONNECT tunnel.

Where a site has HTTP Basic authentication enabled, users obtain their
per-site proxy credentials from the control-plane access page. The proxy
password is shown only on create or rotation and must not be stored in shell
profiles, deployment files, or support tickets. The node applies it through a
normal signed release; it remains available from its last-good configuration
while the control plane is unavailable.

## Local Validation

From the repository root:

```bash
GROUPROXY_TEST_MONGODB_URL='mongodb://<user>:<password>@<host>:<port>/?authSource=admin' \
  GROUPROXY_TEST_GQUAN_APP_TOKEN='sat_<approved-app-token>' \
  GROUPROXY_TESTENV_RESET=1 ./scripts/testenv-up.sh
./scripts/verify-phase1.sh
./scripts/verify-phase2.sh
./scripts/verify-phase4.sh
./scripts/testenv-down.sh
```

Runtime state, tokens, and logs are created under `testenv/` and ignored by
Git. The test script uses the configured codedev MongoDB URI and never starts
or shuts down MongoDB itself. It sends real GQuan verification codes only
after an operator requests one from the login UI. For deterministic auth
regression, use a separate runtime created with
`GROUPROXY_TEST_GQUAN_DELIVERY_MODE=stub`, then run `scripts/verify-auth.sh`.

The test environment disables automatic public probe requests. Its Phase 4
verification stays local unless `GROUPROXY_VERIFY_PROXY_EXTERNAL=1` is set;
that opt-in performs one `HEAD https://www.google.com/ncr` through the proxy.

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
