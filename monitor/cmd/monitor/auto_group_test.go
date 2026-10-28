package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/zl875136491/grouproxy/monitor/internal/config"
	"github.com/zl875136491/grouproxy/monitor/internal/routingdata"
)

func TestSubscriptionAutoGroupRendering(t *testing.T) {
	for _, testCase := range []struct {
		name, selection, expected string
		tags                      []string
	}{
		{name: "default", expected: "auto", tags: []string{"edge-a", "edge-b"}},
		{name: "manual", selection: "edge-b", expected: "edge-b", tags: []string{"edge-a", "edge-b"}},
		{name: "automatic", selection: "auto", expected: "auto", tags: []string{"edge-a"}},
		{name: "stale", selection: "removed", expected: "auto", tags: []string{"edge-a"}},
		{name: "collision", selection: "auto-2", expected: "auto-2", tags: []string{"auto", "auto-1"}},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			stateDir := t.TempDir()
			if err := routingdata.Ensure(stateDir); err != nil {
				t.Fatal(err)
			}
			value := map[string]any{}
			if testCase.selection != "" {
				value["proxy_selection"] = map[string]any{"group": "subscription", "outbound": testCase.selection}
			}
			if err := validateProxySelection(value, testCase.tags); err != nil {
				t.Fatal(err)
			}
			entries := make([]any, 0, len(testCase.tags))
			for _, tag := range testCase.tags {
				entries = append(entries, map[string]any{
					"type": "shadowsocks", "tag": tag, "server": "127.0.0.1",
					"server_port": 8388, "method": "aes-256-gcm", "password": "test-secret",
				})
			}
			rendered := renderSingbox(value, 18080, stateDir, "127.0.0.1:19090", entries, "127.0.0.1", nil)
			outbounds := rendered["outbounds"].([]any)
			automatic := outbounds[len(outbounds)-2].(map[string]any)
			selector := outbounds[len(outbounds)-1].(map[string]any)
			if automatic["type"] != "urltest" || automatic["interrupt_exist_connections"] != false || automatic["interval"] != "1m" {
				t.Fatalf("automatic group = %#v", automatic)
			}
			if !reflect.DeepEqual(automatic["outbounds"], testCase.tags) {
				t.Fatalf("probe targets = %#v", automatic["outbounds"])
			}
			if selector["default"] != testCase.expected || selector["type"] != "selector" {
				t.Fatalf("selector = %#v", selector)
			}
			if !reflect.DeepEqual(selector["outbounds"], append([]string{subscriptionAutoTag(testCase.tags)}, testCase.tags...)) {
				t.Fatalf("selector choices = %#v", selector["outbounds"])
			}
			data, err := json.Marshal(rendered)
			if err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(stateDir, "check.json")
			if err := os.WriteFile(path, data, 0o600); err != nil {
				t.Fatal(err)
			}
			if output, err := exec.Command("../../../singbox/sing-box", "check", "-c", path).CombinedOutput(); err != nil {
				t.Fatalf("sing-box rejected config: %v\n%s", err, output)
			}
		})
	}
}

func TestLastGoodRestoresAutomaticGroupWithoutSubscriptionFetch(t *testing.T) {
	for _, selection := range []string{"", "edge-a"} {
		t.Run("selection="+selection, func(t *testing.T) {
			stateDir := t.TempDir()
			value := map[string]any{}
			if selection != "" {
				value["proxy_selection"] = map[string]any{"group": "subscription", "outbound": selection}
			}
			legacy := map[string]any{"outbounds": []any{
				map[string]any{"type": "direct", "tag": "direct"},
				map[string]any{"type": "shadowsocks", "tag": "edge-a"},
				map[string]any{"type": "selector", "tag": "subscription", "outbounds": []string{"edge-a"}, "default": "edge-a"},
			}}
			data, err := json.Marshal(legacy)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(stateDir, "last-good.json"), data, 0o600); err != nil {
				t.Fatal(err)
			}
			agent := &agent{cfg: config.Config{StateDir: stateDir}}
			for attempt := 0; attempt < 2; attempt++ {
				path, err := agent.ensureLastGoodConfig(value, 3128)
				if err != nil {
					t.Fatal(err)
				}
				data, err = os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				var restored map[string]any
				if err := json.Unmarshal(data, &restored); err != nil {
					t.Fatal(err)
				}
				outbounds := restored["outbounds"].([]any)
				if len(outbounds) != 4 {
					t.Fatalf("restored outbounds = %#v", outbounds)
				}
				expected := selection
				if expected == "" {
					expected = "auto"
				}
				if outbounds[2].(map[string]any)["default"] != expected {
					t.Fatalf("restored selector = %#v", outbounds[2])
				}
			}
		})
	}
}

func TestSubscriptionAutoGroupWithoutSubscriptionKeepsDirect(t *testing.T) {
	rendered := renderSingbox(map[string]any{}, 3128, t.TempDir(), "127.0.0.1:19090", nil, "", nil)
	if rendered["route"].(map[string]any)["final"] != "direct" || len(rendered["outbounds"].([]any)) != 1 {
		t.Fatalf("unexpected direct-only config: %#v", rendered)
	}
	if selectedSubscriptionOutbound(map[string]any{}, nil) != "" {
		t.Fatal("empty subscription has a selection")
	}
}
