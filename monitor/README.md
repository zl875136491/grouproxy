# Grouproxy Monitor

`grouproxy-monitor` is the only component allowed to change node-local
sing-box and proxy-port firewall state. It pulls a Desired Bundle, verifies
version, expiry, hash, HMAC, and minimum monitor version, then runs sing-box
and nftables prechecks before applying the candidate. It samples the proxy
listener and loopback Clash API during the health window and restores
last-good state on failure.

Version `0.3.0` also resolves immutable subscription payloads. It validates
the declared SHA-256 before parsing Clash YAML, SIP008, or sing-box outbound
JSON, and only fetches a large blob from its configured backend with its node
token. The resolved sing-box configuration is persisted as `last-good.json`,
so restart and rollback do not need historical blob access.

When a site requires HTTP Basic authentication, the monitor validates the
signed `proxy_auth` user list and renders only the username/password fields
into its local HTTP inbound. Clash Trojan sources are converted to sing-box
TLS objects, including SNI, certificate-validation mode, ALPN, and client
fingerprint values. The control plane continues to own ACLs, routes, direct /
block outbounds, and selectors.

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
