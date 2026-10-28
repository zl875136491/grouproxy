package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/zl875136491/grouproxy/monitor/internal/config"
	"github.com/zl875136491/grouproxy/monitor/internal/routingdata"
)

func TestDestinationPolicyValidation(t *testing.T) {
	invalid := []string{
		`null`, `{"mode":"global"}`, `{"final":"GLOBAL"}`, `{"unknown":true}`,
		`{"rules":[{"kind":"domain","pattern":"https://example.com","action":"direct"}]}`,
		`{"rules":[{"kind":"domain","pattern":"127.0.0.1","action":"direct"}]}`,
		`{"rules":[{"kind":"domain","pattern":"*.com","action":"direct"}]}`,
		`{"rules":[{"kind":"ip_cidr","pattern":"bad","action":"direct"}]}`,
		`{"rules":[{"kind":"domain","pattern":"example.com","action":"execute"}]}`,
		`{"rules":[{"kind":"domain","pattern":"example.com","action":"proxy","outbound":"GLOBAL"}]}`,
	}
	for _, raw := range invalid {
		var policy any
		if err := json.Unmarshal([]byte(raw), &policy); err != nil {
			t.Fatal(err)
		}
		if _, err := parseDestinationPolicy(map[string]any{"routing_policy": policy}); err == nil {
			t.Fatalf("invalid policy accepted: %s", raw)
		}
	}
}

func TestRuleModeRuntimeDirectProxyAndReject(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		_, _ = io.WriteString(writer, "direct-target")
	}))
	defer target.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if !strings.HasPrefix(request.Host, "child.proxy.example:") {
			writer.WriteHeader(http.StatusBadGateway)
			return
		}
		if request.Method != http.MethodConnect {
			t.Errorf("unexpected upstream method: %s", request.Method)
			return
		}
		connection, buffer, err := writer.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		defer connection.Close()
		_, _ = buffer.WriteString("HTTP/1.1 200 Connection established\r\n\r\n")
		_ = buffer.Flush()
		forwarded, err := http.ReadRequest(buffer.Reader)
		if err != nil {
			t.Error(err)
			return
		}
		_ = forwarded.Body.Close()
		_, _ = buffer.WriteString("HTTP/1.1 200 OK\r\nContent-Length: 14\r\nConnection: close\r\n\r\nproxy-upstream")
		_ = buffer.Flush()
	}))
	defer upstream.Close()
	allocatePort := func() int {
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		port := listener.Addr().(*net.TCPAddr).Port
		_ = listener.Close()
		return port
	}
	proxyPort, apiPort := allocatePort(), allocatePort()
	upstreamPort := upstream.Listener.Addr().(*net.TCPAddr).Port
	stateDir := t.TempDir()
	if err := routingdata.Ensure(stateDir); err != nil {
		t.Fatal(err)
	}
	value := map[string]any{
		"proxy_selection": map[string]any{"group": "subscription", "outbound": "edge"},
		"routing_policy": map[string]any{
			"mode": "rule", "private_direct": true, "cn_direct": false, "final": "direct",
			"rules": []any{
				map[string]any{"kind": "domain_suffix", "pattern": "proxy.example", "action": "proxy"},
				map[string]any{"kind": "domain", "pattern": "blocked.example", "action": "reject"},
				map[string]any{"kind": "domain", "pattern": "localhost", "action": "direct"},
			},
		},
	}
	entries := []any{map[string]any{"type": "http", "tag": "edge", "server": "127.0.0.1", "server_port": upstreamPort}}
	rendered := renderSingbox(value, proxyPort, stateDir, fmt.Sprintf("127.0.0.1:%d", apiPort), entries, "127.0.0.1", nil)
	path := filepath.Join(stateDir, "runtime.json")
	data, _ := json.Marshal(rendered)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	process := exec.Command("../../../singbox/sing-box", "run", "-c", path)
	if err := process.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = process.Process.Kill()
		_ = process.Wait()
	})
	directClient := &http.Client{Transport: &http.Transport{}, Timeout: 2 * time.Second}
	t.Cleanup(directClient.CloseIdleConnections)
	configURL := fmt.Sprintf("http://127.0.0.1:%d/configs", apiPort)
	var mode map[string]any
	for attempt := 0; attempt < 60; attempt++ {
		response, err := directClient.Get(configURL)
		if err == nil {
			_ = json.NewDecoder(response.Body).Decode(&mode)
			_ = response.Body.Close()
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if mode["mode"] != "rule" {
		t.Fatalf("actual Clash mode = %#v", mode)
	}
	proxyURL, _ := url.Parse(fmt.Sprintf("http://127.0.0.1:%d", proxyPort))
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)}, Timeout: 3 * time.Second}
	t.Cleanup(client.CloseIdleConnections)
	for _, testCase := range []struct{ target, expected string }{
		{target.URL, "direct-target"},
		{strings.Replace(target.URL, "127.0.0.1", "localhost", 1), "direct-target"},
		{"http://child.proxy.example/", "proxy-upstream"},
	} {
		response, err := client.Get(testCase.target)
		if err != nil {
			t.Fatalf("request %s: %v", testCase.target, err)
		}
		body, _ := io.ReadAll(response.Body)
		_ = response.Body.Close()
		if string(body) != testCase.expected {
			t.Fatalf("request %s returned %q, want %q", testCase.target, body, testCase.expected)
		}
	}
	response, err := client.Get("http://blocked.example/")
	if err == nil {
		_ = response.Body.Close()
		if response.StatusCode < 400 {
			t.Fatalf("blocked destination returned %d", response.StatusCode)
		}
	}
}

func TestDestinationPolicyRulesAndRestart(t *testing.T) {
	for _, final := range []string{"direct", "proxy"} {
		t.Run(final, func(t *testing.T) {
			stateDir := t.TempDir()
			if err := routingdata.Ensure(stateDir); err != nil {
				t.Fatal(err)
			}
			value := map[string]any{"routing_policy": map[string]any{
				"mode": "rule", "private_direct": true, "cn_direct": false, "final": final,
				"rules": []any{
					map[string]any{"kind": "domain", "pattern": "direct.example", "action": "direct"},
					map[string]any{"kind": "domain_suffix", "pattern": "proxy.example", "action": "proxy"},
					map[string]any{"kind": "ip_cidr", "pattern": "192.0.2.0/24", "action": "reject"},
				},
			}}
			entries := []any{map[string]any{"type": "http", "tag": "edge", "server": "127.0.0.1", "server_port": 8388}}
			access := []sourceBlacklistRule{{Kind: "ip", Pattern: "198.51.100.5", Direction: "source"}}
			rendered := renderSingbox(value, 18080, stateDir, "127.0.0.1:19090", entries, "127.0.0.1", access)
			route := rendered["route"].(map[string]any)
			rules := route["rules"].([]any)
			if len(rules) != 5 || rules[0].(map[string]any)["action"] != "reject" || rules[1].(map[string]any)["outbound"] != "direct" || rules[2].(map[string]any)["outbound"] != "subscription" || rules[3].(map[string]any)["action"] != "reject" || rules[4].(map[string]any)["ip_is_private"] != true {
				t.Fatalf("incorrect rule order: %#v", rules)
			}
			expectedFinal := final
			if final == "proxy" {
				expectedFinal = "subscription"
			}
			if route["final"] != expectedFinal || rendered["experimental"].(map[string]any)["clash_api"].(map[string]any)["default_mode"] != "rule" {
				t.Fatalf("incorrect rule mode: %#v", rendered)
			}
			before, err := json.Marshal(route)
			if err != nil {
				t.Fatal(err)
			}
			if err := applyDestinationPolicy(rendered, value, access); err != nil {
				t.Fatal(err)
			}
			after, _ := json.Marshal(rendered["route"])
			if !reflect.DeepEqual(before, after) {
				t.Fatal("restoring route policy changes its semantics")
			}
			data, _ := json.Marshal(rendered)
			path := filepath.Join(stateDir, "check.json")
			if err := os.WriteFile(path, data, 0o600); err != nil {
				t.Fatal(err)
			}
			if output, err := exec.Command("../../../singbox/sing-box", "check", "-c", path).CombinedOutput(); err != nil {
				t.Fatalf("sing-box config invalid: %v %s", err, output)
			}
		})
	}
}

func TestLastGoodRoutingPolicySurvivesRestart(t *testing.T) {
	stateDir := t.TempDir()
	value := map[string]any{"routing_policy": map[string]any{
		"mode": "rule", "private_direct": false, "cn_direct": false, "final": "direct",
		"rules": []any{map[string]any{"kind": "domain_suffix", "pattern": "example.com", "action": "reject"}},
	}}
	if err := routingdata.Ensure(stateDir); err != nil {
		t.Fatal(err)
	}
	rendered := renderSingbox(value, 18080, stateDir, "127.0.0.1:19090", nil, "127.0.0.1", nil)
	data, _ := json.Marshal(rendered)
	if err := os.WriteFile(filepath.Join(stateDir, "last-good.json"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	agent := &agent{cfg: config.Config{StateDir: stateDir}}
	for attempt := 0; attempt < 2; attempt++ {
		path, err := agent.ensureLastGoodConfig(value, 18080)
		if err != nil {
			t.Fatal(err)
		}
		data, _ := os.ReadFile(path)
		var restored map[string]any
		if err := json.Unmarshal(data, &restored); err != nil {
			t.Fatal(err)
		}
		route := restored["route"].(map[string]any)
		if len(route["rules"].([]any)) != 1 || route["final"] != "direct" || route["rules"].([]any)[0].(map[string]any)["action"] != "reject" {
			t.Fatalf("routing policy lost on restart: %#v", route)
		}
	}
}
