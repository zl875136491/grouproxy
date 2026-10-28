# Import sing-box routing configuration

In Dashboard → Policy → Routing rules, upload a UTF-8 `.json` file or paste JSON.
Download the example template from the same panel. Choose append or replace,
validate and preview, then apply to the editor. Review the current rules in the
collapsible JSON view, then use the upper-right Save routing rules and Publish
routing rules buttons separately. Import/preview does not write
the database, change node desired versions or trigger publication.

Accepted JSON shapes:

- Full sing-box configuration containing `route.rules`.
- A route object: `{"rules": [...], "final": "subscription"}`.
- A bare rules array: `[{"domain_suffix": ["example.com"], "outbound": "direct"}]`.

The current importer covers the site's supported destination-rule subset, not
arbitrary native sing-box configuration. Match fields: `domain`, `domain_suffix`,
`ip_cidr`, each a string or non-empty string array. Arrays and combined supported
destination fields expand into adjacent alternatives with the same action,
preserving source rule priority. Domains/CIDRs use the same validation and
normalization as manually edited rules.

Actions:

```json
{
  "route": {
    "rules": [
      {"domain_suffix": ["example.cn", "internal.example.com"], "action": "route", "outbound": "direct"},
      {"ip_cidr": ["10.0.0.0/8", "192.168.0.0/16"], "outbound": "direct"},
      {"domain": ["www.example.com"], "action": "route", "outbound": "subscription"},
      {"domain_suffix": ["blocked.example"], "action": "reject"}
    ],
    "final": "subscription"
  }
}
```

`action: "route"` can be omitted. `direct` maps to direct routing;
`subscription` and `proxy` both map to the managed subscription selector,
including its automatic URL-test group. Arbitrary outbound tags are rejected:
replace them with `subscription` when they mean the managed proxy, or `direct`.
The import does not add or overwrite subscription endpoints or credentials.

When supplied, `route.final` sets the editor fallback in **both** append and
replace modes; otherwise fallback remains unchanged. Private/China direct
switches remain unchanged, and existing access-control denials remain first.
Append places imported entries after existing custom rules. Replace removes
all current custom rules, including unsaved edits, only after applying the preview.

Unsupported matchers/options (such as ports, source matchers, `invert`, regexes,
logical rules and `rule_set` references), unsupported actions, catch-all rules
and duplicate JSON keys produce errors rather than silently changing semantics.
Use `route.final` for fallback. Only routing rules and final are extracted;
other full-config sections and route options are not imported. Strict JSON is
required (no comments or trailing commas). Maximum UTF-8 file size: 1 MiB;
maximum final expanded custom rules: 200. A leading UTF-8 BOM is accepted.

The management-authenticated stateless endpoint is
`POST /api/v1/routing/import`, accepting
`{"content": "<JSON text>", "mode": "append|replace", "policy": {"mode":"rule", ...}}`.
It returns `{policy, source_rules, imported_rules}` or HTTP 422. The current
unsaved editor policy is the base, not a fresh database policy. The client
invalidates a preview when its source, import mode or base policy changes.
