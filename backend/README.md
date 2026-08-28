# Grouproxy backend

The FastAPI app exposes two intentionally separate surfaces:

- `/api/v1/*` for the management console, protected by the development
  management Bearer token.
- `/agent/v1/*` for monitors, protected by a node Bearer token and signed
  Desired Bundles.

For the supported local environment, run the repository-level script. It
generates isolated test credentials and starts MongoDB before the API:

```bash
./scripts/testenv-up.sh
```

Outside that script, `GROUPROXY_BUNDLE_HMAC_SECRET`,
`GROUPROXY_ADMIN_PASSWORD`, and `GROUPROXY_MANAGEMENT_TOKEN` are mandatory;
the monitor HTTP exception also requires an explicit
`GROUPROXY_ALLOW_INSECURE_AGENT_HTTP=true` opt-in.

The Phase 0/1 implementation covers sites, nodes, source CIDRs, travel and
cross-site policy, drafts, releases, task records, heartbeats, ACKs, audit
hash-chain verification, and the HTTP-only Linux access script. Identity
providers, subscriptions, HTTPS proxy transport, and production password
hashing are later-phase work.
