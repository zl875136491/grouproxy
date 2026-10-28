# Grouproxy Monitor

## Destination routing (0.8.0)

The generated Clash API mode is `rule`, including restored last-good state.
The Dashboard's **Routing rules** page fetches and edits a separate policy per
site. Upgrade every target monitor to 0.8.0 before publishing routing policies.
Saving creates a revisioned draft only; publishing sends its immutable snapshot
through the signed Desired Bundle, precheck, health-check, ACK and rollback path.
Other configuration releases inherit the last published policy, not unsent edits.

Rules match in this order: existing access-control denials, custom rules in table
order, optional private-address direct routing, optional China-domain/IP direct
routing, then the configured fallback. Custom rules support exact domains, domain
suffixes and IP CIDRs, with direct, proxy or reject actions (maximum 200 rules).
The proxy action uses the subscription selector, including automatic URL-test
selection; it does not turn direct routes into proxied traffic. Proxy actions
require an available subscription. Defaults enable private and China direct
routing and use the proxy as fallback. Legacy bundles use those same defaults.

Management-authenticated endpoints are `GET/PUT /api/v1/sites/{site_id}/routing`
and `POST /api/v1/sites/{site_id}/routing/publish`. PUT accepts
`{expected_revision, policy}`; publish accepts `{expected_revision}`. Stale saves
or publishes fail with 409. Use the normal release history to inspect ACKs and
roll back. Apply and publish a policy for each site to cover the whole network.

## Automatic outbound selection (0.7.0)

The generated `subscription` selector now offers an `auto` URL-test group plus
every individual subscription outbound. New bundles without a manual preference
and stale preferences default to `auto`; valid explicit manual choices remain
unchanged. The automatic tag becomes `auto-1`, `auto-2`, etc. when subscription
outbound names collide. Empty subscriptions still use the direct outbound.

The group probes `https://www.gstatic.com/generate_204` every minute while
active, uses a 50 ms tolerance and a 30 minute idle timeout, and does not interrupt
existing connections when switching. It selects by probe latency, not round-robin,
and a successful probe does not guarantee that every destination will work.

Cached last-good configurations are upgraded locally on restart without fetching
the subscription again. Deploy monitor 0.7.0 before enabling automatic selections
in the backend; explicit automatic-selection bundles require monitor 0.7.0.
Rebuild the monitor binary and `dist/SHA256SUMS` with `make dist`. The bundled
sing-box binary does not need to change. The console's existing subscription
cards show the new `auto` / URL-test option alongside manual endpoints.

## Runtime

`grouproxy-monitor` is the only component allowed to change node-local
sing-box and proxy-port firewall state. It pulls a Desired Bundle, verifies
version, expiry, hash, HMAC, and minimum monitor version, then runs sing-box
and nftables prechecks before applying the candidate. It samples the proxy
listener and loopback Clash API during the health window and restores
last-good state on failure.

Version `0.4.0` also resolves immutable subscription payloads. It validates
the declared SHA-256 before parsing Clash YAML, SIP008, or sing-box outbound
JSON, including normalized single-node VMess/VLESS payloads, and only fetches
a large blob from its configured backend with its node token. The resolved
sing-box configuration is persisted as `last-good.json`,
so restart and rollback do not need historical blob access.

Version `0.5.0` changes source access to allow-all by default. Signed bundles
must carry a canonical flat `source_blacklist` array of explicit global or
current-site entries. IP and network entries render as sing-box and nftables
drops; source domains are resolved on the monitor before a candidate is
applied, and the materialized addresses are persisted with last-good state for
DNS-independent rollback. Retired allowlist, destination-block, and source
alias fields are rejected for new bundles and removed from historical local
state before recovery.

Deployed public HTTP inbounds are fixed to port `3128` and have no application
credential list. The same-host test harness may use one explicit alternate
public listener only when `test_ingress_override: true`, the control-plane URL
is loopback HTTP, and firewall mode is `dry-run`; this lets its simulated nuc
node bind `0.0.0.0:18081` without changing deployed-node behavior. Source
blacklist policy is enforced by the rendered route rules and the monitor-owned
nftables policy. Clash Trojan sources are converted to sing-box TLS objects, including
SNI, certificate-validation mode, ALPN, and client fingerprint values. The
control plane owns subscriptions, selector preferences and destination routing
policies; the monitor renders only validated signed policies.

The monitor also samples its loopback Clash `/proxies` endpoint and posts a
bounded proxy-group snapshot to `/agent/v1/proxy-config`. It actively measures
the selectable upstreams through Clash's loopback `/delay` endpoint on a
separate, rate-limited interval, without switching the operator's selected
outbound. Only group names, selection state, protocol flags, and latency
history are included; endpoint servers, credentials, and subscription bytes
are never sent to the control plane. The snapshot is best-effort and uses the
same local telemetry spool as logs and connection summaries.

Health checks inspect Linux listener state through `/proc` before falling back
to a TCP dial on other platforms. This avoids filling sing-box logs with empty
health-check connections while retaining process, listener, and Clash API
status checks.

The monitor embeds pinned local `geoip-cn` and `geosite-cn` binary rule-sets.
They are hash-verified into the monitor state directory and referenced as
local sing-box rule-sets. No node downloads routing data or upstream
subscription URLs at runtime.

Build the production-shaped artifact with:

```bash
make dist
```

The generated `dist/grouproxy-monitor-linux-amd64` and `dist/SHA256SUMS` are
versioned together with monitor code. `-once` is available for isolated node
bootstrap and integration tests. `-validate` loads the monitor configuration
and referenced token file without creating state, contacting the backend, or
changing sing-box/nftables; the systemd unit and node installer use it before
starting the service.
