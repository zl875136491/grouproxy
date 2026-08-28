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
