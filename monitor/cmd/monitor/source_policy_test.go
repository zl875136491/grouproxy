package main

import (
	"context"
	"errors"
	"net"
	"reflect"
	"strings"
	"testing"

	"github.com/zl875136491/grouproxy/monitor/internal/config"
	"github.com/zl875136491/grouproxy/monitor/internal/routingdata"
)

func TestSourceBlacklistRulesSelectsNodeEntries(t *testing.T) {
	value := map[string]any{
		"site_id": "site-a",
		"blacklist": []any{
			map[string]any{"direction": "source", "kind": "ip", "pattern": "192.0.2.1"},
			map[string]any{"direction": "destination", "kind": "cidr", "pattern": "2001:db8::/32"},
		},
	}
	rules := sourceBlacklistRules(value, "site-a")
	if len(rules) != 2 {
		t.Fatalf("rules = %#v, want source and destination entries", rules)
	}
	if rules[0].Kind != "ip" || rules[1].Kind != "cidr" {
		t.Fatalf("rules = %#v", rules)
	}
}

func TestSourceBlacklistRulesIgnoresNestedMigrationShape(t *testing.T) {
	value := map[string]any{
		"blacklist": map[string]any{
			"global": []any{map[string]any{"kind": "domain", "pattern": "global.example"}},
		},
	}
	rules := sourceBlacklistRules(value, "site-a")
	if len(rules) != 0 {
		t.Fatalf("nested migration shape must be ignored, got %#v", rules)
	}
}

func TestSourceBlacklistRulesIgnoresRetiredAliasAndNonCanonicalEntries(t *testing.T) {
	value := map[string]any{
		"blacklist": []any{
			map[string]any{"kind": "ip", "pattern": "192.0.2.1"},
		},
		"deny_sources": []any{
			map[string]any{"direction": "source", "kind": "ip", "pattern": "198.51.100.1"},
		},
	}
	rules := sourceBlacklistRules(value, "site-a")
	if len(rules) != 0 {
		t.Fatalf("retired/noncanonical entries must be ignored, got %#v", rules)
	}
}

func TestRenderSingboxDefaultsToAllowAllSources(t *testing.T) {
	stateDir := t.TempDir()
	if err := routingdata.Ensure(stateDir); err != nil {
		t.Fatalf("ensure routing data: %v", err)
	}
	config := renderSingbox(map[string]any{}, 18080, stateDir, "127.0.0.1:19090", nil, "", nil)
	route := config["route"].(map[string]any)
	for _, raw := range route["rules"].([]any) {
		rule := raw.(map[string]any)
		if _, exists := rule["source_ip_cidr"]; exists {
			t.Fatalf("implicit source IP rule rendered: %#v", rule)
		}
		if _, exists := rule["source_domain"]; exists {
			t.Fatalf("implicit source domain rule rendered: %#v", rule)
		}
	}
}

func TestResolveSourceDomainsAndRenderAsSourceIPs(t *testing.T) {
	originalLookup := lookupSourceDomainIPs
	lookupSourceDomainIPs = func(_ context.Context, domain string) ([]net.IP, error) {
		if domain != "blocked.example" {
			t.Fatalf("lookup domain = %q", domain)
		}
		return []net.IP{net.ParseIP("198.51.100.10"), net.ParseIP("2001:db8::10")}, nil
	}
	defer func() { lookupSourceDomainIPs = originalLookup }()

	raw := []sourceBlacklistRule{
		{Direction: "source", Kind: "ip", Pattern: "192.0.2.1"},
		{Direction: "source", Kind: "domain", Pattern: "blocked.example"},
		{Direction: "destination", Kind: "domain", Pattern: "ads.example"},
	}
	resolved, err := resolveSourceBlacklistRules(context.Background(), raw)
	if err != nil {
		t.Fatalf("resolve source domains: %v", err)
	}
	if len(resolved) != 4 {
		t.Fatalf("resolved rules = %#v", resolved)
	}

	stateDir := t.TempDir()
	if err := routingdata.Ensure(stateDir); err != nil {
		t.Fatalf("ensure routing data: %v", err)
	}
	config := renderSingbox(map[string]any{}, 18080, stateDir, "127.0.0.1:19090", nil, "", resolved)
	route := config["route"].(map[string]any)
	rules := route["rules"].([]any)
	foundIPs := map[string]bool{}
	foundDestDomain := false
	for _, rawRule := range rules {
		rule := rawRule.(map[string]any)
		if _, exists := rule["source_domain"]; exists {
			t.Fatalf("unsupported source_domain field rendered: %#v", rule)
		}
		if values, ok := rule["source_ip_cidr"].([]string); ok && len(values) == 1 {
			foundIPs[values[0]] = true
		}
		if values, ok := rule["domain_suffix"].([]string); ok && len(values) == 1 && values[0] == "ads.example" {
			foundDestDomain = true
		}
	}
	for _, expected := range []string{"192.0.2.1", "198.51.100.10", "2001:db8::10"} {
		if !foundIPs[expected] {
			t.Fatalf("resolved source IP %q missing from route: %#v", expected, rules)
		}
	}
	if !foundDestDomain {
		t.Fatalf("destination domain rule missing from route: %#v", rules)
	}
}

func TestResolveSourceDomainFailureRejectsCandidate(t *testing.T) {
	originalLookup := lookupSourceDomainIPs
	lookupSourceDomainIPs = func(context.Context, string) ([]net.IP, error) {
		return nil, errors.New("lookup failed")
	}
	defer func() { lookupSourceDomainIPs = originalLookup }()

	_, err := resolveSourceBlacklistRules(context.Background(), []sourceBlacklistRule{
		{Direction: "source", Kind: "domain", Pattern: "missing.example"},
	})
	if err == nil || !strings.Contains(err.Error(), "source_domain_resolution_failed") {
		t.Fatalf("resolution error = %v", err)
	}
}

func TestLastGoodSourceRulesUseMaterializedSnapshotDuringDNSFailure(t *testing.T) {
	agent := &agent{cfg: config.Config{StateDir: t.TempDir()}}
	bundleValue := map[string]any{
		"bundle_hash": "last-good-hash",
		"site_id":     "site-a",
		"blacklist": []any{map[string]any{
			"direction": "source", "kind": "domain", "pattern": "blocked.example",
		}},
	}
	materialized := []sourceBlacklistRule{{Direction: "source", Kind: "ip", Pattern: "198.51.100.25"}}
	if err := agent.persistLastGoodSourceBlacklistRules(bundleValue, materialized); err != nil {
		t.Fatalf("persist source snapshot: %v", err)
	}
	originalLookup := lookupSourceDomainIPs
	lookupSourceDomainIPs = func(context.Context, string) ([]net.IP, error) {
		return nil, errors.New("temporary DNS failure")
	}
	defer func() { lookupSourceDomainIPs = originalLookup }()

	got, err := agent.lastGoodSourceBlacklistRules(bundleValue)
	if err != nil {
		t.Fatalf("load materialized source snapshot: %v", err)
	}
	if !reflect.DeepEqual(got, materialized) {
		t.Fatalf("snapshot rules = %#v, want %#v", got, materialized)
	}
}

func TestLastGoodSourceRulesIgnoreSnapshotForLegacyBundle(t *testing.T) {
	agent := &agent{cfg: config.Config{StateDir: t.TempDir()}}
	legacy := map[string]any{
		"bundle_hash": "legacy-hash",
		"site_id":     "site-a",
		"source_blacklist": map[string]any{
			"global": []any{map[string]any{"kind": "ip", "pattern": "192.0.2.1"}},
		},
	}
	materialized := []sourceBlacklistRule{{Direction: "source", Kind: "ip", Pattern: "192.0.2.1"}}
	if err := agent.persistLastGoodSourceBlacklistRules(map[string]any{
		"bundle_hash": "legacy-hash",
		"blacklist":   []any{},
	}, materialized); err != nil {
		t.Fatalf("persist legacy source snapshot: %v", err)
	}

	got, err := agent.lastGoodSourceBlacklistRules(legacy)
	if err != nil {
		t.Fatalf("read legacy source snapshot: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("legacy snapshot must be ignored, got %#v", got)
	}
}

func TestLastGoodSourceRulesIgnoreSnapshotWhenRetiredAliasRemains(t *testing.T) {
	agent := &agent{cfg: config.Config{StateDir: t.TempDir()}}
	legacy := map[string]any{
		"bundle_hash": "legacy-alias-hash",
		"site_id":     "site-a",
		"blacklist":   []any{},
		"deny_sources": []any{},
	}
	materialized := []sourceBlacklistRule{{Direction: "source", Kind: "ip", Pattern: "192.0.2.1"}}
	if err := agent.persistLastGoodSourceBlacklistRules(legacy, materialized); err != nil {
		t.Fatalf("persist legacy alias source snapshot: %v", err)
	}
	got, err := agent.lastGoodSourceBlacklistRules(legacy)
	if err != nil {
		t.Fatalf("read legacy alias source snapshot: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("legacy alias snapshot must be ignored, got %#v", got)
	}
}

func TestEnsureSourceBlacklistRulesRemovesRetiredInvertedRule(t *testing.T) {
	config := map[string]any{
		"route": map[string]any{
			"rules": []any{
				map[string]any{"source_ip_cidr": []any{"10.0.0.0/8"}, "invert": true, "action": "reject"},
				map[string]any{"action": "reject"},
				map[string]any{"domain": []any{"blocked.example"}, "outbound": "block"},
			},
		},
		"outbounds": []any{
			map[string]any{"type": "direct", "tag": "direct"},
			map[string]any{"type": "block", "tag": "block"},
		},
	}
	if !ensureSourceBlacklistRules(config, []sourceBlacklistRule{{Direction: "source", Kind: "ip", Pattern: "192.0.2.1"}}) {
		t.Fatal("expected persisted route migration")
	}
	rules := config["route"].(map[string]any)["rules"].([]any)
	if len(rules) != 1 {
		t.Fatalf("rules = %#v", rules)
	}
	if !reflect.DeepEqual(rules[0].(map[string]any)["source_ip_cidr"], []string{"192.0.2.1"}) {
		t.Fatalf("new source rule = %#v", rules[0])
	}
	if _, stale := rules[0].(map[string]any)["invert"]; stale {
		t.Fatal("retired invert matcher survived migration")
	}
	for _, raw := range config["outbounds"].([]any) {
		outbound := raw.(map[string]any)
		if outbound["type"] == "block" && outbound["tag"] == "block" {
			t.Fatalf("retired destination block outbound survived migration: %#v", outbound)
		}
	}
}

func TestSanitizeLegacyBundleRemovesRetiredPolicyFields(t *testing.T) {
	value := map[string]any{
		"allow_cidrs":       []any{"10.0.0.0/8"},
		"deny_destinations": []any{map[string]any{"kind": "domain", "pattern": "blocked.example"}},
		"deny_sources":      []any{map[string]any{"kind": "ip", "pattern": "192.0.2.1"}},
		"source_blacklist": map[string]any{
			"global": []any{map[string]any{"kind": "ip", "pattern": "192.0.2.2"}},
		},
	}
	if !sanitizeLegacyBundle(value) {
		t.Fatal("expected legacy policy fields to be removed")
	}
	for _, key := range []string{"allow_cidrs", "deny_destinations", "deny_sources", "source_blacklist"} {
		if _, exists := value[key]; exists {
			t.Fatalf("retired policy field %q survived sanitization", key)
		}
	}
	if raw, exists := value["blacklist"].([]any); !exists || len(raw) != 0 {
		t.Fatalf("legacy source blacklist was not reset to the empty canonical baseline: %#v", value["blacklist"])
	}
	if !hasRetiredSourcePolicy(map[string]any{"deny_sources": []any{}}) {
		t.Fatal("retired alias was not detected for snapshot invalidation")
	}

	canonical := map[string]any{
		"blacklist": []any{map[string]any{
			"direction": "source", "kind": "ip", "pattern": "192.0.2.10",
		}},
	}
	if hasRetiredSourcePolicy(canonical) || sanitizeLegacyBundle(canonical) {
		t.Fatalf("canonical blacklist was unexpectedly sanitized: %#v", canonical)
	}
}
