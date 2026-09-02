package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/zl875136491/grouproxy/monitor/internal/config"
)

func proxyConfigTestAgent(serverURL string) *agent {
	return &agent{cfg: config.Config{ClashAPIListen: strings.TrimPrefix(serverURL, "http://")}}
}

func writeProxyResponse(t *testing.T, writer http.ResponseWriter, value any) {
	t.Helper()
	writer.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(writer).Encode(value); err != nil {
		t.Fatalf("encode response: %v", err)
	}
}

func TestReadProxyGroupsProjectsOnlySafeFields(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/proxies" {
			http.NotFound(writer, request)
			return
		}
		writeProxyResponse(t, writer, map[string]any{
			"proxies": map[string]any{
				"subscription": map[string]any{
					"name":     "subscription",
					"type":     "Selector",
					"now":      "edge-a",
					"all":      []string{"edge-a", "edge-b", "edge-a"},
					"udp":      true,
					"delay":    123,
					"password": "secret-must-not-leak",
				},
				"edge-a": map[string]any{
					"type":     "Trojan",
					"alive":    true,
					"delay":    45,
					"server":   "198.51.100.20",
					"password": "secret-must-not-leak",
					"uuid":     "secret-must-not-leak",
				},
			},
		})
	}))
	defer server.Close()

	groups, err := proxyConfigTestAgent(server.URL).readProxyGroups()
	if err != nil {
		t.Fatalf("readProxyGroups() error = %v", err)
	}
	if len(groups) != 1 {
		t.Fatalf("group count = %d, want 1", len(groups))
	}
	encoded, err := json.Marshal(groups)
	if err != nil {
		t.Fatal(err)
	}
	serialized := string(encoded)
	for _, secret := range []string{"198.51.100.20", "secret-must-not-leak", "password", "uuid", "server"} {
		if strings.Contains(serialized, secret) {
			t.Fatalf("projection contains %q: %s", secret, serialized)
		}
	}
	group := groups[0].(map[string]any)
	if group["now"] != "edge-a" || group["delay_ms"] != 123 {
		t.Fatalf("group metadata = %#v", group)
	}
	nodes := group["nodes"].([]any)
	if len(nodes) != 2 {
		t.Fatalf("node count = %d, want 2", len(nodes))
	}
}

func TestProbeProxyURLUsesCurrentLocalInboundCredentials(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "sing-box.json")
	if err := os.WriteFile(configPath, []byte(`{"inbounds":[{"type":"http","tag":"grouproxy-http","users":[{"username":"monitor","password":"local-secret"}]}]}`), 0o600); err != nil {
		t.Fatal(err)
	}

	proxyURL, err := (&agent{cfg: config.Config{ListenPort: 18080, SingboxConfig: configPath}}).probeProxyURL()
	if err != nil {
		t.Fatalf("probeProxyURL() error = %v", err)
	}
	if proxyURL.Host != "127.0.0.1:18080" {
		t.Fatalf("proxy host = %q", proxyURL.Host)
	}
	if proxyURL.User == nil || proxyURL.User.Username() != "monitor" {
		t.Fatalf("proxy credentials were not loaded")
	}
	password, present := proxyURL.User.Password()
	if !present || password != "local-secret" {
		t.Fatalf("proxy password was not loaded")
	}
}

func TestReadProxyGroupsCollectsDelaysForSelectableEndpoints(t *testing.T) {
	delayCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/proxies":
			writeProxyResponse(t, writer, map[string]any{
				"proxies": map[string]any{
					"subscription": map[string]any{
						"name": "subscription", "type": "Selector", "all": []string{"edge a", "edge b", "direct"},
					},
					"edge a": map[string]any{"type": "Trojan"},
					"edge b": map[string]any{"type": "Trojan"},
					"direct": map[string]any{"type": "Direct"},
				},
			})
		case "/proxies/edge a/delay":
			delayCalls++
			if got := request.URL.Query().Get("url"); got != proxyDelayTargetURL {
				t.Fatalf("delay URL = %q", got)
			}
			if got := request.URL.Query().Get("timeout"); got != strconv.Itoa(proxyDelayTimeoutMilliseconds) {
				t.Fatalf("delay timeout = %q", got)
			}
			writeProxyResponse(t, writer, map[string]any{"delay": 47})
		case "/proxies/edge b/delay":
			delayCalls++
			writer.WriteHeader(http.StatusGatewayTimeout)
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	agent := proxyConfigTestAgent(server.URL)
	agent.cfg.ProxyDelayIntervalSeconds = 60
	groups, err := agent.readProxyGroups()
	if err != nil {
		t.Fatalf("readProxyGroups() error = %v", err)
	}
	if delayCalls != 2 {
		t.Fatalf("delay calls = %d, want 2", delayCalls)
	}
	nodes := groups[0].(map[string]any)["nodes"].([]any)
	endpoint := nodes[0].(map[string]any)
	if endpoint["name"] != "edge a" || endpoint["delay_ms"] != 47 {
		t.Fatalf("endpoint projection = %#v", endpoint)
	}
	history := endpoint["history"].([]any)
	if len(history) != 1 || history[0].(map[string]any)["delay_ms"] != 47 {
		t.Fatalf("endpoint history = %#v", history)
	}
	failed := nodes[1].(map[string]any)
	if failed["name"] != "edge b" || failed["alive"] != false || failed["delay_ms"] != nil {
		t.Fatalf("failed endpoint projection = %#v", failed)
	}
}

func TestReadProxyGroupsFallsBackToSubscriptionSelector(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/proxies":
			writeProxyResponse(t, writer, map[string]any{
				"proxies": map[string]any{
					"direct": map[string]any{"type": "Direct", "name": "direct"},
				},
			})
		case "/proxies/subscription":
			writeProxyResponse(t, writer, map[string]any{
				"type": "Selector", "name": "subscription", "now": "edge-a", "all": []string{"edge-a"},
			})
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	groups, err := proxyConfigTestAgent(server.URL).readProxyGroups()
	if err != nil {
		t.Fatalf("readProxyGroups() error = %v", err)
	}
	if len(groups) != 1 || groups[0].(map[string]any)["name"] != "subscription" {
		t.Fatalf("fallback groups = %#v", groups)
	}
}

func TestReadProxyGroupsAddsSubscriptionSelectorBesideObservedGroups(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/proxies":
			writeProxyResponse(t, writer, map[string]any{
				"proxies": map[string]any{
					"GLOBAL": map[string]any{"type": "Fallback", "name": "GLOBAL", "now": "edge-a", "all": []string{"edge-a"}},
				},
			})
		case "/proxies/subscription":
			writeProxyResponse(t, writer, map[string]any{
				"type": "Selector", "name": "subscription", "now": "edge-a", "all": []string{"edge-a"},
			})
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	groups, err := proxyConfigTestAgent(server.URL).readProxyGroups()
	if err != nil {
		t.Fatalf("readProxyGroups() error = %v", err)
	}
	if len(groups) != 2 || groups[1].(map[string]any)["name"] != "subscription" {
		t.Fatalf("groups = %#v, want observed group plus subscription selector", groups)
	}
}

func TestReadProxyGroupsFallsBackWhenProxyCollectionEndpointIsMissing(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/proxies":
			http.NotFound(writer, request)
		case "/proxies/subscription":
			writeProxyResponse(t, writer, map[string]any{
				"type": "Selector", "name": "subscription", "now": "edge-a", "all": []string{"edge-a"},
			})
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	groups, err := proxyConfigTestAgent(server.URL).readProxyGroups()
	if err != nil {
		t.Fatalf("readProxyGroups() error = %v", err)
	}
	if len(groups) != 1 || groups[0].(map[string]any)["name"] != "subscription" {
		t.Fatalf("fallback groups = %#v", groups)
	}
}

func TestReadProxyGroupsAcceptsValidResponseWithNoSelectableGroup(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/proxies" {
			writeProxyResponse(t, writer, map[string]any{
				"proxies": map[string]any{
					"direct": map[string]any{"type": "Direct", "name": "direct"},
					"block":  map[string]any{"type": "Reject", "name": "block"},
				},
			})
			return
		}
		http.NotFound(writer, request)
	}))
	defer server.Close()

	groups, err := proxyConfigTestAgent(server.URL).readProxyGroups()
	if err != nil {
		t.Fatalf("readProxyGroups() error = %v", err)
	}
	if len(groups) != 0 {
		t.Fatalf("groups = %#v, want empty", groups)
	}
}

func TestReadProxyGroupsRejectsMalformedResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/proxies" {
			writeProxyResponse(t, writer, map[string]any{"proxies": map[string]any{"broken": "not-an-object"}})
			return
		}
		http.NotFound(writer, request)
	}))
	defer server.Close()

	_, err := proxyConfigTestAgent(server.URL).readProxyGroups()
	if err == nil || err.Error() != "clash_api_invalid_response" {
		t.Fatalf("error = %v, want clash_api_invalid_response", err)
	}
}

func TestProxyConfigErrorClassifiesAvailability(t *testing.T) {
	if got := proxyConfigError(nil); got != "" {
		t.Fatalf("nil error = %q", got)
	}
	if got := proxyConfigError(errors.New("clash_api_invalid_response")); got != "clash_api_invalid_response" {
		t.Fatalf("invalid response error = %q", got)
	}
	if got := proxyConfigError(errors.New("connection refused")); got != "clash_api_unavailable" {
		t.Fatalf("unavailable error = %q", got)
	}
}

func TestProxyConfigGroupsMarshalAsArrayWhenUnavailable(t *testing.T) {
	payload := map[string]any{"groups": proxyConfigGroups(nil)}
	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	if _, ok := decoded["groups"].([]any); !ok {
		t.Fatalf("groups must marshal as an array, got %#v", decoded["groups"])
	}
}

func TestSelectedSubscriptionOutboundUsesControlPlaneChoice(t *testing.T) {
	value := map[string]any{
		"proxy_selection": map[string]any{
			"group":    "subscription",
			"outbound": "edge-b",
		},
	}
	if got := selectedSubscriptionOutbound(value, []string{"edge-a", "edge-b"}); got != "edge-b" {
		t.Fatalf("selected outbound = %q, want edge-b", got)
	}
	if got := selectedSubscriptionOutbound(map[string]any{}, []string{"edge-a", "edge-b"}); got != "edge-a" {
		t.Fatalf("fallback outbound = %q, want edge-a", got)
	}
}

func TestValidateProxySelectionRejectsUnmappableChoice(t *testing.T) {
	if err := validateProxySelection(map[string]any{
		"proxy_selection": map[string]any{"group": "subscription", "outbound": "edge-b"},
	}, []string{"edge-a", "edge-b"}); err != nil {
		t.Fatalf("valid selection rejected: %v", err)
	}

	for _, value := range []map[string]any{
		{"proxy_selection": map[string]any{"group": "GLOBAL", "outbound": "edge-a"}},
		{"proxy_selection": map[string]any{"group": "subscription", "outbound": "missing"}},
	} {
		if err := validateProxySelection(value, []string{"edge-a"}); err == nil {
			t.Fatalf("invalid selection accepted: %#v", value)
		}
	}
}
