package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
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
