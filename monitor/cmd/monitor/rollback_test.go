package main

import (
	"bytes"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/zl875136491/grouproxy/monitor/internal/client"
	"github.com/zl875136491/grouproxy/monitor/internal/config"
	"github.com/zl875136491/grouproxy/monitor/internal/routingdata"
	"github.com/zl875136491/grouproxy/monitor/internal/runtime"
	"github.com/zl875136491/grouproxy/monitor/internal/state"
)

func TestRollbackUsesPortOverrideAndKeepsLastGoodConfig(t *testing.T) {
	var ackBody []byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/agent/v1/ack" {
			t.Fatalf("unexpected request path %s", r.URL.Path)
		}
		ackBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"accepted":true}`))
	}))
	defer server.Close()

	dir := t.TempDir()
	binary := filepath.Join(dir, "fake-sing-box")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\nexit 0\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	lastGood := map[string]any{
		"listen":      map[string]any{"http_port": 80},
		"allow_cidrs": []string{"10.32.12.0/24"},
		"shutdown":    false,
	}
	agent := &agent{
		cfg: config.Config{
			StateDir:           dir,
			SingboxConfig:      filepath.Join(dir, "sing-box.json"),
			ListenPortOverride: 18080,
			ClashAPIListen:     "127.0.0.1:19090",
			FirewallMode:       "dry-run",
		},
		client:   &client.Client{BaseURL: server.URL, Token: "test", HTTPClient: server.Client()},
		state:    state.State{LastGoodBundle: lastGood, LastGoodVersion: 4, LastGoodHash: "last-good", AppliedVersion: 4, AppliedHash: "last-good"},
		sequence: 7,
		runtime:  &runtime.Manager{Binary: binary, StateDir: dir, RunProcess: false},
		log:      log.New(bytes.NewBuffer(nil), "", 0),
	}
	candidate := map[string]any{"release_id": "candidate", "desired_version": 5, "bundle_hash": "candidate-hash"}

	if err := agent.rollback(candidate, "candidate.json", "health_window_failed", false, false, false); err == nil {
		t.Fatal("rollback should report the rejected candidate")
	}
	configData, err := os.ReadFile(agent.cfg.SingboxConfig)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(configData, []byte(`"listen_port": 18080`)) {
		t.Fatalf("rollback did not preserve listen_port_override:\n%s", configData)
	}
	if agent.state.ConfigStatus != "failed" || agent.state.ServiceStatus != "healthy" {
		t.Fatalf("rollback state = %s/%s", agent.state.ConfigStatus, agent.state.ServiceStatus)
	}
	if !bytes.Contains(ackBody, []byte(`"rollback_attempted":true`)) || !bytes.Contains(ackBody, []byte(`"rollback_ok":true`)) {
		t.Fatalf("rollback ACK missing outcome: %s", ackBody)
	}
}

func TestRenderSingboxUsesSubscriptionForNonCNTraffic(t *testing.T) {
	stateDir := t.TempDir()
	if err := routingdata.Ensure(stateDir); err != nil {
		t.Fatalf("ensure routing data: %v", err)
	}
	config := renderSingbox(
		map[string]any{
			"allow_cidrs":       []string{"10.32.12.0/24"},
			"deny_destinations": []any{map[string]any{"kind": "domain", "pattern": "blocked.test"}},
		},
		18080,
		stateDir,
		"127.0.0.1:19090",
		[]any{map[string]any{
			"type":        "shadowsocks",
			"tag":         "edge-a",
			"server":      "198.51.100.20",
			"server_port": 8388,
			"method":      "aes-256-gcm",
			"password":    "secret",
		}},
	)
	route := config["route"].(map[string]any)
	if route["final"] != "subscription" {
		t.Fatalf("route final = %v", route["final"])
	}
	foundSelector := false
	for _, raw := range config["outbounds"].([]any) {
		outbound := raw.(map[string]any)
		if outbound["tag"] == "subscription" && outbound["type"] == "selector" {
			foundSelector = true
		}
	}
	if !foundSelector {
		t.Fatal("subscription selector missing")
	}
	foundCNDirect := false
	for _, raw := range route["rules"].([]any) {
		rule := raw.(map[string]any)
		tags, ok := rule["rule_set"].([]string)
		if ok && len(tags) == 2 && tags[0] == routingdata.GeoIPCNTag && tags[1] == routingdata.GeoSiteCNTag && rule["outbound"] == "direct" {
			foundCNDirect = true
		}
	}
	if !foundCNDirect {
		t.Fatal("CN direct-routing rule missing")
	}
	ruleSets := route["rule_set"].([]any)
	if len(ruleSets) != 2 {
		t.Fatalf("rule-set count = %d, want 2", len(ruleSets))
	}
}
