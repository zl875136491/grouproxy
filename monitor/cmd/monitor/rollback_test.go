package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/zl875136491/grouproxy/monitor/internal/client"
	"github.com/zl875136491/grouproxy/monitor/internal/config"
	"github.com/zl875136491/grouproxy/monitor/internal/firewall"
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
		"listen":           map[string]any{"http_port": 1080},
		"blacklist": []any{},
		"shutdown":         false,
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
			"blacklist": []any{map[string]any{"direction": "source", "kind": "ip", "pattern": "192.0.2.1"}},
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
		"",
		[]sourceBlacklistRule{{Scope: "global", Kind: "ip", Pattern: "192.0.2.1"}},
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
	inbound := config["inbounds"].([]any)[0].(map[string]any)
	if _, exists := inbound["users"]; exists {
		t.Fatalf("proxy authentication fields unexpectedly rendered: %#v", inbound["users"])
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

func TestRenderSingboxSupportsLoopbackIngress(t *testing.T) {
	stateDir := t.TempDir()
	if err := routingdata.Ensure(stateDir); err != nil {
		t.Fatalf("ensure routing data: %v", err)
	}
	config := renderSingbox(
		map[string]any{"blacklist": []any{}},
		18080,
		stateDir,
		"127.0.0.1:19090",
		nil,
		"127.0.0.1",
		nil,
	)
	inbound := config["inbounds"].([]any)[0].(map[string]any)
	if inbound["listen"] != "127.0.0.1" || inbound["listen_port"] != 18080 {
		t.Fatalf("unexpected internal ingress: %#v", inbound)
	}
	if _, exists := inbound["proxy_protocol"]; exists {
		t.Fatalf("unsupported PROXY protocol option rendered: %#v", inbound)
	}
}

func TestEnsureLastGoodConfigReappliesOperationalIngress(t *testing.T) {
	stateDir := t.TempDir()
	if err := routingdata.Ensure(stateDir); err != nil {
		t.Fatalf("ensure routing data: %v", err)
	}
	lastGood := map[string]any{
		"blacklist": []any{},
		"shutdown":         false,
	}
	persisted := renderSingbox(lastGood, 18080, stateDir, "127.0.0.1:19090", nil, "", nil)
	persistedData, err := json.Marshal(persisted)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stateDir, "last-good.json"), persistedData, 0o600); err != nil {
		t.Fatal(err)
	}
	agent := &agent{cfg: config.Config{
		StateDir:              stateDir,
		ListenAddressOverride: "127.0.0.1",
	}}
	path, err := agent.ensureLastGoodConfig(lastGood, 18080)
	if err != nil {
		t.Fatalf("ensure last-good config: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var restored map[string]any
	if err := json.Unmarshal(data, &restored); err != nil {
		t.Fatal(err)
	}
	inbound := restored["inbounds"].([]any)[0].(map[string]any)
	if inbound["listen"] != "127.0.0.1" {
		t.Fatalf("operational ingress was not restored: %#v", inbound)
	}
}

func TestApplySingboxIngressRemovesRetiredProxyUsers(t *testing.T) {
	configValue := map[string]any{
		"inbounds": []any{
			map[string]any{
				"type": "http",
				"users": []any{
					map[string]any{"username": "legacy", "password": "secret"},
				},
			},
		},
	}

	applySingboxIngress(configValue, 1080, false, "")
	inbound := configValue["inbounds"].([]any)[0].(map[string]any)
	if _, exists := inbound["users"]; exists {
		t.Fatalf("retired proxy users were retained: %#v", inbound["users"])
	}
	if inbound["listen_port"] != 1080 {
		t.Fatalf("listen port = %v, want 1080", inbound["listen_port"])
	}
}

func TestSanitizeLegacyBundleRemovesAuthAndPinsPort(t *testing.T) {
	bundleValue := map[string]any{
		"proxy_auth": map[string]any{"required": true},
		"listen":     map[string]any{"http_port": 80},
	}

	if !sanitizeLegacyBundle(bundleValue) {
		t.Fatal("legacy bundle was not marked changed")
	}
	if _, exists := bundleValue["proxy_auth"]; exists {
		t.Fatal("proxy_auth was retained")
	}
	if got := bundleValue["listen"].(map[string]any)["http_port"]; got != 1080 {
		t.Fatalf("listen port = %v, want 1080", got)
	}
}

func TestRestoreLastGoodFirewallUsesOverridePort(t *testing.T) {
	agent := &agent{cfg: config.Config{StateDir: t.TempDir(), FirewallPortOverride: 18080, FirewallMode: "dry-run"}}
	lastGood := map[string]any{
		"listen":           map[string]any{"http_port": 1080},
		"blacklist": []any{},
	}
	if err := agent.restoreLastGoodFirewallForBundle(lastGood); err != nil {
		t.Fatalf("render last-good firewall: %v", err)
	}
	script := firewall.RenderSourceRules(agent.firewallPort(1080), nil, false)
	if !strings.Contains(script, "tcp dport 18080") || strings.Contains(script, "tcp dport 1080") {
		t.Fatalf("firewall override was not selected:\n%s", script)
	}
}

func TestAllowAllFirewallBaselineUsesOverridePort(t *testing.T) {
	port, script := allowAllFirewallBaseline(config.Config{
		ListenPort:           1080,
		FirewallPortOverride: 18080,
	})
	if port != 18080 {
		t.Fatalf("baseline port = %d, want 18080", port)
	}
	if !strings.Contains(script, "tcp dport 18080 accept") || strings.Contains(script, "saddr") || strings.Contains(script, " drop") {
		t.Fatalf("baseline did not clear source policy:\n%s", script)
	}
}
