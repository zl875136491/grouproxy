# Grouproxy Monitor

`grouproxy-monitor` is the only component allowed to change node-local
sing-box and proxy-port firewall state. It pulls a Desired Bundle, verifies
version, expiry, hash, HMAC, and minimum monitor version, then runs sing-box
and nftables prechecks before applying the candidate. It samples the proxy
listener and loopback Clash API during the health window and restores
last-good state on failure.

Version `0.2.1` also resolves immutable subscription payloads. It validates
the declared SHA-256 before parsing Clash YAML, SIP008, or sing-box outbound
JSON, and only fetches a large blob from its configured backend with its node
token. The resolved sing-box configuration is persisted as `last-good.json`,
so restart and rollback do not need historical blob access.

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
bootstrap and integration tests.
