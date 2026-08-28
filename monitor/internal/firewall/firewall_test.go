package firewall

import (
	"strings"
	"testing"
)

func TestRenderIsFailClosedAndScoped(t *testing.T) {
	script := Render(80, []string{"10.0.0.0/8", "2001:db8::/32"}, false)
	if !strings.Contains(script, "destroy table inet grouproxy") {
		t.Fatal("Render must be checkable on a fresh host")
	}
	for _, expected := range []string{
		"ip saddr 10.0.0.0/8 tcp dport 80 accept",
		"ip6 saddr 2001:db8::/32 tcp dport 80 accept",
		"tcp dport 80 drop",
	} {
		if !strings.Contains(script, expected) {
			t.Fatalf("rendered firewall missing %q:\n%s", expected, script)
		}
	}
}

func TestRenderShutdownDropsWithoutAllowRules(t *testing.T) {
	script := Render(18080, []string{"10.0.0.0/8"}, true)
	if strings.Contains(script, "saddr") || !strings.Contains(script, "tcp dport 18080 drop") {
		t.Fatalf("shutdown rules are not fail-closed:\n%s", script)
	}
}
