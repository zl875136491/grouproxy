# Grouproxy monitor

`grouproxy-monitor` is the only component allowed to change node-local
sing-box and proxy-port firewall state. It pulls a Desired Bundle, verifies
version/expiry/hash/HMAC, runs sing-box and nftables prechecks, applies the
candidate, samples its proxy listener and loopback Clash API during the health
window, and preserves a last-good bundle on failure.

Build the production-shaped artifact with:

```bash
make dist
```

The generated `dist/grouproxy-monitor-linux-amd64` and `dist/SHA256SUMS` are
versioned together with monitor code. `-once` is available for isolated node
bootstrap and integration tests.
