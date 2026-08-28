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
