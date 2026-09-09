package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadRequiresExplicitOptInForHTTP(t *testing.T) {
	path := filepath.Join(t.TempDir(), "monitor.yaml")
	secret := "01234567890123456789012345678901"
	if err := os.WriteFile(path, []byte("backend_url: http://127.0.0.1:8000\nnode_id: n\ntoken_file: /tmp/t\nsingbox_bin: /tmp/s\nhmac_secret: \""+secret+"\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Fatal("HTTP backend accepted without allow_insecure_http")
	}
	if err := os.WriteFile(path, []byte("backend_url: http://127.0.0.1:8000\nnode_id: n\ntoken_file: /tmp/t\nsingbox_bin: /tmp/s\nhmac_secret: \""+secret+"\"\nallow_insecure_http: true\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err != nil {
		t.Fatalf("HTTP opt-in rejected: %v", err)
	}
}

func TestLoadRejectsNonLoopbackClashAPI(t *testing.T) {
	path := filepath.Join(t.TempDir(), "monitor.yaml")
	content := "backend_url: https://control.example\nnode_id: n\ntoken_file: /tmp/t\nsingbox_bin: /tmp/s\nhmac_secret: \"01234567890123456789012345678901\"\nclash_api_listen: 10.0.0.5:9090\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Fatal("non-loopback Clash API listener accepted")
	}
}

func TestLoadRestrictsIngressOverridesToExplicitTestModes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "monitor.yaml")
	base := "backend_url: https://control.example\nnode_id: n\ntoken_file: /tmp/t\nsingbox_bin: /tmp/s\nhmac_secret: \"01234567890123456789012345678901\"\n"
	if err := os.WriteFile(path, []byte(base+"listen_address_override: invalid\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Fatal("invalid ingress address was accepted")
	}
	if err := os.WriteFile(path, []byte(base+"listen_port: 18080\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Fatal("non-standard production listener accepted")
	}
	if err := os.WriteFile(path, []byte(base+"listen_address_override: 127.0.0.1\nlisten_port_override: 18080\nfirewall_port_override: 18080\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("loopback ingress overrides rejected: %v", err)
	}
	if cfg.ListenPortOverride != 18080 || cfg.FirewallPortOverride != 18080 || cfg.ListenAddressOverride != "127.0.0.1" {
		t.Fatalf("operational ingress overrides were not loaded: %#v", cfg)
	}
	if err := os.WriteFile(path, []byte(base+"listen_address_override: 127.0.0.1\nlisten_port_override: 18080\nfirewall_port_override: 18080\nfirewall_mode: apply\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Fatal("publicly applicable ingress override accepted")
	}
	if err := os.WriteFile(path, []byte(base+"listen_address_override: 0.0.0.0\nlisten_port_override: 18080\nfirewall_port_override: 18080\ntest_ingress_override: true\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Fatal("public ingress accepted without loopback test backend")
	}
	publicTestBase := "backend_url: http://127.0.0.1:8000\nnode_id: n\ntoken_file: /tmp/t\nsingbox_bin: /tmp/s\nhmac_secret: \"01234567890123456789012345678901\"\nallow_insecure_http: true\n"
	if err := os.WriteFile(path, []byte(publicTestBase+"listen_address_override: 0.0.0.0\nlisten_port_override: 18080\nfirewall_port_override: 18080\ntest_ingress_override: true\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err = Load(path)
	if err != nil {
		t.Fatalf("explicit public test ingress rejected: %v", err)
	}
	if !cfg.TestIngressOverride || cfg.ListenAddressOverride != "0.0.0.0" {
		t.Fatalf("public test ingress was not loaded: %#v", cfg)
	}
}
