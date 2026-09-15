# Grouproxy Deployment

One control plane runs on `codedev`; `codedev` and `nuc` can both run a monitor
and its sing-box child process. The monitor fetches signed Desired Bundles over
the configured agent channel and owns both sing-box and the proxy-port
nftables policy.

## Direct Listeners

No forwarding layer is used.

- Each proxy domain points directly at its node's HTTP CONNECT listener on
  TCP `1080`.
- The dashboard is a separate Next.js process exposed directly on TCP `80`.
  Its built-in `/api/*` rewrite sends dashboard API calls to the private FastAPI
  backend listener.
- The proxy has no HTTP Basic authentication. Source access is allow-all by
  default; explicit global or site-scoped source blacklist rules are the only
  network deny boundary.

The access page serves immutable, pre-generated workstation assets. The
`test` environment uses `test-proxy.1oa.com.cn`; production uses
`proxy.1oa.com.cn`. Set `GROUPROXY_ENVIRONMENT=test` in the backend process to
select the test pair; every other value selects production. The endpoint is
always HTTP CONNECT on TCP `1080` and has no proxy authentication layer.

`linux-setup-proxy.sh` and `linux-setup-proxy-test.sh` are parameterless
current-user toggles. Each run detects the current shell/desktop proxy state:
it enables the HTTP CONNECT proxy when disabled and turns it off when enabled.
They update GNOME or KDE when their settings tools are available. The dashboard
downloads the matching file from `GET /api/v1/access/linux-setup.sh` and the
matching PowerShell file from `GET /api/v1/access/windows-setup.ps1`.
Keep the `deploy/` directory beside the deployed backend package (for example
`/opt/grouproxy/deploy`) because the backend reads these checked-in files
directly; do not replace them with request-time templates.

The Windows asset reads the current user's WinINET `ProxyEnable` setting and
reverses it without parameters: it writes the environment-specific proxy when
enabling and turns the system proxy off when it is already enabled. It does not
install a certificate or proxy credentials. macOS uses the environment-specific
`.shortcut` download shown on the access page.

HTTPS destinations remain end-to-end inside the HTTP CONNECT tunnel.

The monitor systemd unit retains `CAP_NET_BIND_SERVICE` for the proxy listener
and `CAP_NET_ADMIN` for the proxy-port nftables policy. The dashboard service,
if it is run as a non-root user, likewise needs permission to bind `:80`.

## Dashboard Installation

Build the dashboard with the private backend URL, then deploy its standalone
artifact and enable the direct listener:

```bash
cd frontend
GROUPROXY_BACKEND_API_URL=http://127.0.0.1:8000 npm ci
GROUPROXY_BACKEND_API_URL=http://127.0.0.1:8000 npm run build

sudo install -d -m 0755 /opt/grouproxy/dashboard
sudo cp -a .next/standalone/. /opt/grouproxy/dashboard/
sudo chmod -R a+rX /opt/grouproxy/dashboard
sudo install -m 0644 ../deploy/grouproxy-dashboard.service /etc/systemd/system/grouproxy-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now grouproxy-dashboard.service
```

The build copies `.next/static` and `public/` into the standalone artifact, so
the service can run without `next start`, an external reverse proxy, or the
source `node_modules` directory. `GROUPROXY_BACKEND_API_URL` is a build-time
value because Next.js writes the `/api/*` rewrite into the production output.
The dashboard also forwards `/healthz` and `/readyz`; after restarting both
processes, `curl http://<dashboard-domain>/healthz` must report backend version
`0.5.0`. A 404 from `/api/v1/subscriptions/single-node` means the running
backend/dashboard artifact is older than this source tree, not that the URI
failed validation.

After updating the backend source or monitor artifact, restart every process
that may still hold the previous version before testing a new publication:

```bash
sudo systemctl restart grouproxy-backend.service
sudo systemctl restart grouproxy-dashboard.service
sudo systemctl restart grouproxy-monitor.service
```

Use the actual backend unit name used by the host if it differs. A successful
single-node request requires the management Bearer session and returns `201`;
missing authentication returns `401`, while malformed VLESS/VMess data returns
`422` with a `single_node_*` error code.

Interactive management sessions last 30 days by default. Set
`GROUPROXY_AUTH_SESSION_TTL_MINUTES=43200` explicitly in the backend environment
when deployment configuration should pin that value. Changing it affects newly
created sessions; an already expired or revoked session must sign in again.

Remove the retired Grouproxy NGINX entrypoint only after the direct service is
healthy on port `80`:

```bash
sudo systemctl disable --now nginx
sudo rm -f /etc/nginx/conf.d/grouproxy-dashboard.conf \
  /etc/nginx/modules-enabled/99-grouproxy-entrypoint.conf \
  /etc/nginx/njs/grouproxy-stream.js
```

## Local Validation

From the repository root:

```bash
GROUPROXY_TEST_MONGODB_URL='mongodb://<user>:<password>@<codedev-host>:<port>/?authSource=admin' \
  GROUPROXY_TEST_GQUAN_APP_TOKEN='sat_<approved-app-token>' \
  GROUPROXY_TESTENV_RESET=1 ./scripts/testenv-up.sh
./scripts/verify-phase1.sh
./scripts/verify-phase2.sh
./scripts/verify-phase3.sh
./scripts/verify-phase4.sh
./scripts/testenv-down.sh
```

Runtime state, tokens, and logs are created under `testenv/` and ignored by
Git. Its client-facing dashboard and both proxy simulations bind to network
addresses: codedev uses `:1080` and the same-host nuc simulation uses `:18081`.
The latter is an explicit test-only exception to avoid two processes binding
the same port. A deployed nuc host uses its own address on `:1080`.

## Remote Node Installation

`install-node.sh` requires an explicit SSH user and optional key:

```bash
NUC_SSH_USER=operator NUC_SSH_KEY=/path/to/key \
  ./deploy/install-node.sh 10.32.12.110
```

Use `DRY_RUN=1` to inspect the node commands. The script copies only monitor,
sing-box, and systemd artifacts; it does not install the control plane or a
dashboard. Before enabling, it runs the monitor's local `-validate` mode as
`grouproxy`; this checks `monitor.yaml` and the referenced non-empty token
without contacting the backend. If validation fails, artifacts are left
installed but the service is not enabled or started. Create the files, then
run:

```bash
sudo systemctl enable --now grouproxy-monitor.service
```
