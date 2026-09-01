package main

import (
	"bytes"
	"context"
	"log"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/zl875136491/grouproxy/monitor/internal/client"
	"github.com/zl875136491/grouproxy/monitor/internal/config"
	"github.com/zl875136491/grouproxy/monitor/internal/state"
)

func TestSyncClearsOnlyTransientControlPlaneError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/agent/v1/desired" {
			t.Fatalf("unexpected request path %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"desired_stale":false}`))
	}))
	defer server.Close()

	agent := &agent{
		cfg: config.Config{NodeID: "node-a"},
		client: &client.Client{
			BaseURL:    server.URL,
			Token:      "test",
			HTTPClient: server.Client(),
		},
		state:     state.State{LastError: "candidate rejected", ConfigStatus: "failed"},
		syncError: "dial tcp 127.0.0.1:8000: connect: connection refused",
		log:       log.New(bytes.NewBuffer(nil), "", 0),
	}

	if err := agent.sync(context.Background()); err != nil {
		t.Fatalf("sync after control-plane recovery: %v", err)
	}
	if agent.syncError != "" {
		t.Fatalf("sync error was not cleared: %q", agent.syncError)
	}
	if agent.state.LastError != "candidate rejected" {
		t.Fatalf("configuration error was unexpectedly cleared: %q", agent.state.LastError)
	}
}

func TestHeartbeatStatusSeparatesLivenessAndConfigurationFailures(t *testing.T) {
	status, lastError := heartbeatStatus(state.State{LastError: "bundle validation failed"}, "", true, true)
	if status != "online" || lastError != "bundle validation failed" {
		t.Fatalf("healthy last-good after config failure = %q, %q", status, lastError)
	}

	status, lastError = heartbeatStatus(state.State{}, "connection refused", true, true)
	if status != "degraded" || lastError != "connection refused" {
		t.Fatalf("control-plane outage = %q, %q", status, lastError)
	}

	status, _ = heartbeatStatus(state.State{}, "", false, true)
	if status != "degraded" {
		t.Fatalf("stopped proxy status = %q", status)
	}
}
