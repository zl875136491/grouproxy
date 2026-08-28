package firewall

import (
	"fmt"
	"os/exec"
	"strings"
)

func Render(port int, cidrs []string, shutdown bool) string {
	var lines []string
	// `destroy` is accepted when the table does not exist, so the exact same
	// script can be dry-run on a fresh node and applied on an existing node.
	lines = append(lines, "destroy table inet grouproxy")
	lines = append(lines, "table inet grouproxy {")
	lines = append(lines, "  chain grouproxy_input {")
	lines = append(lines, "    type filter hook input priority -100; policy accept;")
	if !shutdown {
		for _, cidr := range cidrs {
			if strings.Contains(cidr, ":") {
				lines = append(lines, fmt.Sprintf("    ip6 saddr %s tcp dport %d accept", cidr, port))
			} else {
				lines = append(lines, fmt.Sprintf("    ip saddr %s tcp dport %d accept", cidr, port))
			}
		}
	}
	lines = append(lines, fmt.Sprintf("    tcp dport %d drop", port))
	lines = append(lines, "  }")
	lines = append(lines, "}")
	return strings.Join(lines, "\n") + "\n"
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
