package firewall

import (
	"strings"
	"testing"
)

func TestRenderSourceRulesBlocksListedSourcesAndAllowsTheRest(t *testing.T) {
	script := RenderSourceRules(1080, []SourceRule{
		{Kind: "network", Pattern: "10.0.0.0/8"},
		{Kind: "network", Pattern: "2001:db8::/32"},
	}, false)
	if !strings.Contains(script, "destroy table inet grouproxy") {
		t.Fatal("RenderSourceRules must be checkable on a fresh host")
	}
	for _, expected := range []string{
		"ip saddr 10.0.0.0/8 tcp dport 1080 drop",
		"ip6 saddr 2001:db8::/32 tcp dport 1080 drop",
		"tcp dport 1080 accept",
	} {
		if !strings.Contains(script, expected) {
			t.Fatalf("rendered firewall missing %q:\n%s", expected, script)
		}
	}
}

func TestRenderShutdownDropsWithoutAllowRules(t *testing.T) {
	script := RenderSourceRules(18080, []SourceRule{{Kind: "network", Pattern: "10.0.0.0/8"}}, true)
	if !strings.Contains(script, "tcp dport 18080 drop") || strings.Contains(script, "tcp dport 18080 accept") {
		t.Fatalf("shutdown rules are not fail-closed:\n%s", script)
	}
}

func TestRenderSourceRulesAllowsUnlistedSources(t *testing.T) {
	script := RenderSourceRules(1080, nil, false)
	if strings.Contains(script, "saddr") || strings.Contains(script, " drop") {
		t.Fatalf("empty source blacklist unexpectedly blocks traffic:\n%s", script)
	}
	if !strings.Contains(script, "tcp dport 1080 accept") {
		t.Fatalf("empty source blacklist is not allow-all:\n%s", script)
	}
}

func TestRenderSourceRulesDropsOnlyIPNetworks(t *testing.T) {
	script := RenderSourceRules(1080, []SourceRule{
		{Kind: "ip", Pattern: "192.0.2.10"},
		{Kind: "network", Pattern: "2001:db8::/32"},
		{Kind: "domain", Pattern: "blocked.example"},
	}, false)
	for _, expected := range []string{
		"ip saddr 192.0.2.10 tcp dport 1080 drop",
		"ip6 saddr 2001:db8::/32 tcp dport 1080 drop",
		"tcp dport 1080 accept",
	} {
		if !strings.Contains(script, expected) {
			t.Fatalf("rendered source blacklist missing %q:\n%s", expected, script)
		}
	}
	if strings.Contains(script, "blocked.example") {
		t.Fatalf("domain source leaked into nft syntax:\n%s", script)
	}
}

func TestRenderSourceRulesShutdownStillDropsAll(t *testing.T) {
	script := RenderSourceRules(18080, nil, true)
	if !strings.Contains(script, "tcp dport 18080 drop") || strings.Contains(script, "tcp dport 18080 accept") {
		t.Fatalf("shutdown did not block listener:\n%s", script)
	}
}
