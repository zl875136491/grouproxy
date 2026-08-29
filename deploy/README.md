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
GROUPROXY_TEST_MONGODB_URL='mongodb://<user>:<password>@<host>:<port>/?authSource=admin' \
  GROUPROXY_TEST_GQUAN_APP_TOKEN='sat_<approved-app-token>' \
  GROUPROXY_TESTENV_RESET=1 ./scripts/testenv-up.sh
./scripts/verify-phase1.sh
./scripts/verify-phase2.sh
./scripts/testenv-down.sh
```

Runtime state, tokens, and logs are created under `testenv/` and ignored by
Git. The test script uses the configured codedev MongoDB URI and never starts
or shuts down MongoDB itself. It sends real GQuan verification codes only
after an operator requests one from the login UI. For deterministic auth
regression, use a separate runtime created with
`GROUPROXY_TEST_GQUAN_DELIVERY_MODE=stub`, then run `scripts/verify-auth.sh`.

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
