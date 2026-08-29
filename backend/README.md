# Grouproxy Backend

The FastAPI application exposes separate management and agent surfaces:

- `/api/v1/*` is used by the operations dashboard and accepts an opaque
  browser session created by `/api/v1/auth/*`. The static management Bearer
  token remains available for the existing local node-validation scripts.
- `/agent/v1/*` is used by monitors and requires a node Bearer token. Desired
  Bundles are additionally protected by an HMAC.

For the supported test environment, configure the codedev MongoDB URI and run
the repository-level script. It creates local runtime credentials but does not
start MongoDB:

```bash
GROUPROXY_TEST_MONGODB_URL='mongodb://<user>:<password>@<host>:<port>/?authSource=admin' \
  GROUPROXY_TESTENV_RESET=1 ./scripts/testenv-up.sh
```

Outside that script, `GROUPROXY_BUNDLE_HMAC_SECRET`,
`GROUPROXY_ADMIN_PASSWORD`, and `GROUPROXY_MANAGEMENT_TOKEN` are mandatory.
The HTTP agent channel also requires an explicit
`GROUPROXY_ALLOW_INSECURE_AGENT_HTTP=true` opt-in.

For One Login verification delivery, configure
`GROUPROXY_GQUAN_APP_TOKEN` with an approved APP Bearer token. The default API
base is `https://one.1oa.com.cn/springboard/api/v1`; this is an outbound call
to One Login, not a TLS listener in Grouproxy. The default test profile also
uses the real APP API, accepting its token only as the runtime
`GROUPROXY_TEST_GQUAN_APP_TOKEN` input. Do not place either token in the
repository, audit records, browser configuration, or test fixture.

Phase 0/1 covers sites, nodes, source CIDRs, travel and cross-site policy,
drafts, releases, ACKs, tasks, and the audit hash chain. Phase 2 adds
`subscription_source`, immutable `subscription_version`, and per-site
selection records plus these management operations:

- list/register/refresh HTTP sources and upload a source file;
- publish a parsed version to selected sites and create a normal release;
- roll back a site's selected version through the same release and ACK path;
- serve a selected large blob only to the node whose Desired Bundle references
  its hash.

Subscription URLs are HTTP-only, have no embedded credentials, and are never
returned by the API. Fetching validates all DNS results and every redirect
against SSRF targets. Uploaded sources are intentionally immutable and are not
scheduled for refresh. Refresh work uses an active-task partial unique index,
lease recovery, backoff, cancellation, and dead-letter state. Publish and
rollback derive stable per-site task keys from `Idempotency-Key`, so retries
return the existing releases without overwriting rollback history.

Authentication uses `itcode` as the primary account identity. Registration,
password changes, and passwordless GQuan login all consume a single-use
verification challenge. Challenges are rate-limited, HMAC-digested, and
short-lived; browser access tokens are opaque server-side sessions and become
invalid when the password changes.
