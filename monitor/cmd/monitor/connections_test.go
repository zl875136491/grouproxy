package main

import (
	"testing"
	"time"
)

func TestFillConnectionSnapshotProjectsRequestIP(t *testing.T) {
	now := time.Date(2026, time.September, 15, 12, 0, 0, 0, time.UTC)
	snapshot := map[string]any{}
	raw := map[string]any{
		"uploadTotal":   float64(120),
		"downloadTotal": float64(340),
		"connections": []any{
			map[string]any{
				"id":       "connection-1",
				"start":    "2026-09-15T11:59:00Z",
				"upload":   float64(12),
				"download": float64(34),
				"metadata": map[string]any{
					"sourceIP":        "192.0.2.44",
					"sourcePort":      "53124",
					"destinationIP":   "203.0.113.7",
					"destinationPort": "443",
					"host":            "example.test",
					"network":         "tcp",
					"type":            "HTTP",
				},
			},
		},
	}

	fillConnectionSnapshot(snapshot, raw, now, &agent{})
	connections, ok := snapshot["connections"].([]map[string]any)
	if !ok || len(connections) != 1 {
		t.Fatalf("connections = %#v", snapshot["connections"])
	}
	if got := connections[0]["src_ip"]; got != "192.0.2.44" {
		t.Fatalf("src_ip = %#v, want request IP", got)
	}
	if got := snapshot["top_sources"].([]any)[0].(map[string]any)["label"]; got != "192.0.2.44" {
		t.Fatalf("top source = %#v, want request IP", got)
	}
}

func TestFillConnectionSnapshotAcceptsSnakeCaseRequestIP(t *testing.T) {
	snapshot := map[string]any{}
	raw := map[string]any{
		"connections": []any{
			map[string]any{
				"metadata": map[string]any{"source_ip": "198.51.100.9"},
			},
		},
	}

	fillConnectionSnapshot(snapshot, raw, time.Now().UTC(), &agent{})
	connections := snapshot["connections"].([]map[string]any)
	if got := connections[0]["src_ip"]; got != "198.51.100.9" {
		t.Fatalf("src_ip = %#v, want normalized request IP", got)
	}
}
