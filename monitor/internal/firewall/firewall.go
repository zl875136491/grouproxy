package firewall

import (
	"fmt"
	"net"
	"os/exec"
	"strings"
)

// SourceRule describes a source-side blacklist entry. Domain entries cannot
// be enforced by nftables and are intentionally ignored by the renderer;
// sing-box applies those entries at the proxy route layer.
type SourceRule struct {
	Kind    string
	Pattern string
}

// RenderSourceRules renders source blacklist drops while leaving every other
// source able to reach the proxy. This is deliberately fail-open for source
// policy: a missing/empty blacklist must never turn into an implicit
// allowlist or a catch-all reject.
func RenderSourceRules(port int, rules []SourceRule, shutdown bool) string {
	var lines []string
	// `destroy` is accepted when the table does not exist, so the exact same
	// script can be dry-run on a fresh node and applied on an existing node.
	lines = append(lines, "destroy table inet grouproxy")
	lines = append(lines, "table inet grouproxy {")
	lines = append(lines, "  chain grouproxy_input {")
	lines = append(lines, "    type filter hook input priority -100; policy accept;")
	for _, rule := range rules {
		family, pattern, ok := normalizeSourceRule(rule)
		if !ok {
			continue
		}
		lines = append(lines, fmt.Sprintf("    %s saddr %s tcp dport %d drop", family, pattern, port))
	}
	if shutdown {
		// Site shutdown remains an explicit operational override. It blocks the
		// proxy listener while leaving unrelated host traffic untouched.
		lines = append(lines, fmt.Sprintf("    tcp dport %d drop", port))
	} else {
		// Make the all-source behavior visible and stable even if the chain is
		// later extended with additional rules.
		lines = append(lines, fmt.Sprintf("    tcp dport %d accept", port))
	}
	lines = append(lines, "  }")
	lines = append(lines, "}")
	return strings.Join(lines, "\n") + "\n"
}

func normalizeSourceRule(rule SourceRule) (family, pattern string, ok bool) {
	pattern = strings.TrimSpace(rule.Pattern)
	if pattern == "" {
		return "", "", false
	}
	kind := strings.ToLower(strings.TrimSpace(rule.Kind))
	switch kind {
	case "ip":
		address := net.ParseIP(pattern)
		if address == nil {
			return "", "", false
		}
		if address.To4() != nil {
			return "ip", address.To4().String(), true
		}
		return "ip6", address.String(), true
	case "network":
		address, network, err := net.ParseCIDR(pattern)
		if err != nil || address == nil || network == nil {
			return "", "", false
		}
		if address.To4() != nil {
			return "ip", network.String(), true
		}
		return "ip6", network.String(), true
	default:
		// Domain source rules are enforced by sing-box, where the HTTP proxy
		// request context is available. Never interpolate them into nft syntax.
		return "", "", false
	}
}

func Check(script string) error {
	cmd := exec.Command("nft", "-c", "-f", "-")
	cmd.Stdin = strings.NewReader(script)
	if output, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("nft dry-run: %w: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}

func Apply(script string) error {
	cmd := exec.Command("nft", "-f", "-")
	cmd.Stdin = strings.NewReader(script)
	if output, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("nft apply: %w: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}
