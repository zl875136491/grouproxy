package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/zl875136491/grouproxy/monitor/internal/bundle"
	"github.com/zl875136491/grouproxy/monitor/internal/client"
	"github.com/zl875136491/grouproxy/monitor/internal/config"
	"github.com/zl875136491/grouproxy/monitor/internal/firewall"
	"github.com/zl875136491/grouproxy/monitor/internal/routingdata"
	"github.com/zl875136491/grouproxy/monitor/internal/runtime"
	"github.com/zl875136491/grouproxy/monitor/internal/state"
	"github.com/zl875136491/grouproxy/monitor/internal/subscription"
)

const (
	monitorVersion                = "0.3.0"
	proxyDelayTargetURL           = "https://www.gstatic.com/generate_204"
	proxyDelayTimeoutMilliseconds = 5_000
	proxyDelayConcurrency         = 6
	maxProxyDelayTargets          = 500
)

type agent struct {
	cfg               config.Config
	client            *client.Client
	state             state.State
	runtime           *runtime.Manager
	sequence          int
	log               *log.Logger
	stateMu           sync.Mutex
	probeMu           sync.Mutex
	proxyConfigMu     sync.Mutex
	lastProxyConfigAt time.Time
	lastProxyDelayAt  time.Time
	// syncError only represents the latest inability to reach the control plane.
	// Configuration failures are persisted in state.LastError and reported on the
	// configuration dimension without making a healthy monitor look offline.
	syncError string
}

type clashSelector struct {
	Type string   `json:"type"`
	Name string   `json:"name"`
	Now  string   `json:"now"`
	All  []string `json:"all"`
	UDP  bool     `json:"udp"`
}

type spoolEnvelope struct {
	Kind      string          `json:"kind"`
	Priority  int             `json:"priority"`
	CreatedAt time.Time       `json:"created_at"`
	Payload   json.RawMessage `json:"payload"`
}

type accessLogEntry struct {
	TS            time.Time `json:"ts"`
	PolicyVersion int       `json:"policy_version"`
	SrcIP         string    `json:"src_ip"`
	SrcCIDRMatch  string    `json:"src_cidr_match"`
	Username      string    `json:"username"`
	CertFP        string    `json:"cert_fp"`
	DstHost       string    `json:"dst_host"`
	DstPort       int       `json:"dst_port"`
	Action        string    `json:"action"`
	DenyReason    string    `json:"deny_reason"`
	BytesUp       int64     `json:"bytes_up"`
	BytesDown     int64     `json:"bytes_down"`
	DurationMS    int64     `json:"duration_ms"`
}

func main() {
	configPath := flag.String("config", "/etc/grouproxy/monitor.yaml", "monitor configuration")
	once := flag.Bool("once", false, "fetch and apply once, then exit")
	validate := flag.Bool("validate", false, "validate configuration and token, then exit")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("load config: %v", err)
	}
	if *validate {
		if _, err := client.New(cfg.BackendURL, cfg.TokenFile); err != nil {
			log.Fatalf("validate agent credentials: %v", err)
		}
		return
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		log.Fatalf("create state directory: %v", err)
	}
	stateValue, err := state.Load(cfg.StateDir)
	if err != nil {
		log.Fatalf("load state: %v", err)
	}
	apiClient, err := client.New(cfg.BackendURL, cfg.TokenFile)
	if err != nil {
		log.Fatalf("create agent client: %v", err)
	}
	a := &agent{
		cfg:      cfg,
		client:   apiClient,
		state:    stateValue,
		sequence: stateValue.Sequence,
		runtime: &runtime.Manager{
			Binary: cfg.SingboxBin, ConfigPath: cfg.SingboxConfig, StateDir: cfg.StateDir,
			ListenPort: cfg.ListenPort, APIAddress: cfg.ClashAPIListen, RunProcess: cfg.RunSingbox,
		},
		log: log.New(os.Stdout, "grouproxy-monitor ", log.LstdFlags|log.LUTC),
	}
	defer a.runtime.Close()

	if err := a.restoreLastGood(); err != nil {
		a.log.Printf("last-good restore skipped: %v", err)
	}
	if *once {
		if err := a.sync(context.Background()); err != nil {
			a.log.Printf("sync failed: %v", err)
			os.Exit(1)
		}
		return
	}
	a.run(context.Background())
}

func (a *agent) run(ctx context.Context) {
	pollTicker := time.NewTicker(time.Duration(a.cfg.PollIntervalSeconds) * time.Second)
	heartbeatTicker := time.NewTicker(time.Duration(a.cfg.HeartbeatIntervalSeconds) * time.Second)
	defer pollTicker.Stop()
	defer heartbeatTicker.Stop()

	_ = a.sync(ctx)
	_ = a.sendHeartbeat(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-pollTicker.C:
			if err := a.sync(ctx); err != nil {
				a.log.Printf("sync failed: %v", err)
			}
		case <-heartbeatTicker.C:
			if err := a.sendHeartbeat(ctx); err != nil {
				a.log.Printf("heartbeat failed: %v", err)
			}
		}
	}
}

func (a *agent) nextTelemetrySequence(kind string) (int, string) {
	a.stateMu.Lock()
	defer a.stateMu.Unlock()
	var sequence int
	switch kind {
	case "logs":
		a.state.LogSequence++
		sequence = a.state.LogSequence
	case "connections":
		a.state.ConnectionSequence++
		sequence = a.state.ConnectionSequence
	case "probes":
		a.state.ProbeSequence++
		sequence = a.state.ProbeSequence
	case "proxy_config":
		a.state.ProxyConfigSequence++
		sequence = a.state.ProxyConfigSequence
	}
	_ = state.Save(a.cfg.StateDir, a.state)
	return sequence, fmt.Sprintf("%s-%s-%d-%d", a.cfg.NodeID, kind, sequence, time.Now().UnixNano())
}

func (a *agent) telemetrySpoolDir() string {
	return filepath.Join(a.cfg.StateDir, "spool")
}

func (a *agent) enqueueSpool(kind string, payload any, priority int) error {
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	envelope, err := json.Marshal(spoolEnvelope{
		Kind: kind, Priority: priority, CreatedAt: time.Now().UTC(), Payload: data,
	})
	if err != nil {
		return err
	}
	dir := a.telemetrySpoolDir()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	// Keep deny batches ahead of sampled allow/connection data when the cap is
	// reached. A single oversized batch is dropped rather than bypassing the
	// bound.
	if int64(len(envelope))+directorySize(dir) > a.cfg.SpoolMaxBytes {
		_ = a.trimSpool(int64(len(envelope)))
	}
	if int64(len(envelope))+directorySize(dir) > a.cfg.SpoolMaxBytes {
		return errors.New("telemetry_spool_full")
	}
	name := fmt.Sprintf("%d-%020d-%s.json", priority, time.Now().UnixNano(), kind)
	tmp, err := os.CreateTemp(dir, ".spool-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return err
	}
	if _, err := tmp.Write(envelope); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, filepath.Join(dir, name))
}

func (a *agent) trimSpool(required int64) error {
	dir := a.telemetrySpoolDir()
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	type spoolFile struct {
		path     string
		priority int
		size     int64
	}
	files := make([]spoolFile, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		info, statErr := entry.Info()
		if statErr != nil {
			continue
		}
		priority := 1
		if strings.HasPrefix(entry.Name(), "0-") {
			priority = 0
		}
		files = append(files, spoolFile{filepath.Join(dir, entry.Name()), priority, info.Size()})
	}
	// Evict sampled data first, oldest first. Deny data is retained until no
	// other option exists, and even then the hard cap is respected.
	sort.Slice(files, func(i, j int) bool {
		if files[i].priority != files[j].priority {
			return files[i].priority > files[j].priority
		}
		return files[i].path < files[j].path
	})
	current := directorySize(dir)
	for _, file := range files {
		if current+required <= a.cfg.SpoolMaxBytes {
			break
		}
		if err := os.Remove(file.path); err == nil {
			current -= file.size
		}
	}
	return nil
}

func (a *agent) replaySpool() {
	dir := a.telemetrySpoolDir()
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() && strings.HasSuffix(entry.Name(), ".json") {
			names = append(names, entry.Name())
		}
	}
	sort.Strings(names)
	for _, name := range names {
		path := filepath.Join(dir, name)
		data, readErr := os.ReadFile(path)
		if readErr != nil {
			continue
		}
		var envelope spoolEnvelope
		if json.Unmarshal(data, &envelope) != nil {
			_ = os.Remove(path)
			continue
		}
		var payload map[string]any
		if json.Unmarshal(envelope.Payload, &payload) != nil {
			_ = os.Remove(path)
			continue
		}
		var sendErr error
		switch envelope.Kind {
		case "logs":
			sendErr = a.client.Logs(payload)
		case "connections":
			sendErr = a.client.Connections(payload)
		case "proxy_config":
			sendErr = a.client.ProxyConfig(payload)
		case "probes":
			sendErr = a.client.Probes(payload)
		default:
			_ = os.Remove(path)
			continue
		}
		if sendErr != nil {
			return
		}
		_ = os.Remove(path)
	}
}

func (a *agent) sendTelemetry(ctx context.Context) {
	_ = ctx
	a.replaySpool()
	a.collectLogs()
	a.collectConnections()
	a.collectProxyConfig()
}

func (a *agent) collectLogs() {
	path := filepath.Join(a.cfg.StateDir, "sing-box.log")
	file, err := os.Open(path)
	if err != nil {
		return
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return
	}
	a.stateMu.Lock()
	offset := a.state.LogOffset
	if offset > info.Size() {
		// sing-box may truncate the file in place during rotation. Resetting the
		// cursor prevents a seek past EOF from permanently skipping new entries.
		offset = 0
		a.state.LogOffset = 0
		_ = state.Save(a.cfg.StateDir, a.state)
	}
	a.stateMu.Unlock()
	if _, err := file.Seek(offset, io.SeekStart); err != nil {
		return
	}
	reader := bufio.NewReaderSize(file, 64*1024)
	entries := make([]accessLogEntry, 0, a.cfg.TelemetryBatchMax)
	newOffset := offset
	for len(entries) < a.cfg.TelemetryBatchMax {
		line, readErr := reader.ReadBytes('\n')
		if len(line) == 0 && readErr != nil {
			break
		}
		if readErr == io.EOF && (len(line) == 0 || line[len(line)-1] != '\n') {
			// Keep a partial final line for the next collection pass.
			break
		}
		newOffset += int64(len(line))
		line = bytes.TrimSuffix(line, []byte{'\n'})
		line = bytes.TrimSuffix(line, []byte{'\r'})
		if parsed, ok := parseAccessLog(line); ok {
			entries = append(entries, parsed)
		}
	}
	a.stateMu.Lock()
	if newOffset > a.state.LogOffset {
		a.state.LogOffset = newOffset
		_ = state.Save(a.cfg.StateDir, a.state)
	}
	a.stateMu.Unlock()
	if len(entries) == 0 {
		return
	}
	sequence, batchID := a.nextTelemetrySequence("logs")
	payload := map[string]any{"node_id": a.cfg.NodeID, "batch_id": batchID, "sequence": sequence, "entries": entries}
	if err := a.client.Logs(payload); err != nil {
		priority := 1
		for _, entry := range entries {
			if entry.Action == "deny" {
				priority = 0
				break
			}
		}
		if spoolErr := a.enqueueSpool("logs", payload, priority); spoolErr != nil {
			a.log.Printf("log telemetry dropped: %v", spoolErr)
		}
	}
}

func parseAccessLog(line []byte) (accessLogEntry, bool) {
	entry := accessLogEntry{TS: time.Now().UTC(), Action: "allow"}
	isDeny := false
	var raw map[string]any
	if json.Unmarshal(line, &raw) == nil {
		if value := stringValue(raw["action"]); value == "allow" || value == "deny" {
			entry.Action = value
			isDeny = value == "deny"
		}
		if value := stringValue(raw["timestamp"]); value != "" {
			if parsed, err := time.Parse(time.RFC3339Nano, value); err == nil {
				entry.TS = parsed
			}
		} else if value := stringValue(raw["time"]); value != "" {
			if parsed, err := time.Parse(time.RFC3339Nano, value); err == nil {
				entry.TS = parsed
			}
		}
		entry.SrcIP = stringValue(raw["src_ip"])
		entry.DstHost = stripQuery(stringValue(raw["dst_host"]))
		entry.DstPort, _ = asInt(raw["dst_port"])
		entry.Username = stringValue(raw["username"])
		entry.DenyReason = stringValue(raw["deny_reason"])
		entry.BytesUp = asInt64(raw["bytes_up"])
		entry.BytesDown = asInt64(raw["bytes_down"])
		entry.DurationMS = asInt64(raw["duration_ms"])
		message := strings.ToLower(stringValue(raw["message"]))
		if entry.Action == "allow" && isAuthenticationFailure(message) {
			entry.Action = "deny"
			isDeny = true
			if entry.DenyReason == "" {
				entry.DenyReason = "auth_failed"
			}
		} else if entry.Action == "allow" && (strings.Contains(message, "deny") || strings.Contains(message, "reject") || strings.Contains(message, "blocked")) {
			entry.Action = "deny"
			isDeny = true
			if entry.DenyReason == "" {
				entry.DenyReason = "other"
			}
		}
	} else {
		message := strings.ToLower(string(line))
		if isAuthenticationFailure(message) {
			entry.Action = "deny"
			entry.DenyReason = "auth_failed"
		} else if !strings.Contains(message, "deny") && !strings.Contains(message, "reject") && !strings.Contains(message, "blocked") {
			// Allow logs are sampled to approximately one percent.
			hash := sha256.Sum256(line)
			if hash[0]%100 != 0 {
				return accessLogEntry{}, false
			}
		} else {
			entry.Action = "deny"
			entry.DenyReason = "other"
		}
	}
	if entry.Action == "allow" && !isDeny {
		// Allow telemetry is intentionally sampled at approximately one
		// percent; deny telemetry remains complete for policy investigations.
		hash := sha256.Sum256(line)
		if hash[0]%100 != 0 {
			return accessLogEntry{}, false
		}
	}
	entry.SrcIP = strings.Join(strings.Fields(entry.SrcIP), "")[:minLen(len(strings.Join(strings.Fields(entry.SrcIP), "")), 64)]
	entry.Username = strings.Join(strings.Fields(entry.Username), "")[:minLen(len(strings.Join(strings.Fields(entry.Username), "")), 128)]
	entry.DstHost = stripQuery(entry.DstHost)
	return entry, true
}

func isAuthenticationFailure(message string) bool {
	return strings.Contains(message, "authentication failed") ||
		strings.Contains(message, "auth failed") ||
		strings.Contains(message, "proxy authentication") ||
		strings.Contains(message, "unauthorized")
}

func stripQuery(value string) string {
	if parsed, err := url.Parse(value); err == nil && parsed.Hostname() != "" {
		return parsed.Hostname()
	}
	return strings.Split(value, "?")[0]
}

func minLen(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func asInt64(value any) int64 {
	switch typed := value.(type) {
	case int:
		return int64(typed)
	case int64:
		return typed
	case float64:
		return int64(typed)
	case json.Number:
		parsed, _ := strconv.ParseInt(string(typed), 10, 64)
		return parsed
	default:
		return 0
	}
}

func (a *agent) collectConnections() {
	endpoint := "http://" + a.cfg.ClashAPIListen + "/connections"
	request, err := http.NewRequest(http.MethodGet, endpoint, nil)
	if err != nil {
		return
	}
	request.Header.Set("Accept", "application/json")
	client := &http.Client{Timeout: 800 * time.Millisecond}
	snapshot := map[string]any{
		"sampled_at":         time.Now().UTC(),
		"active_connections": 0,
		"bytes_up":           int64(0),
		"bytes_down":         int64(0),
		"top_sources":        []any{},
		"top_destinations":   []any{},
		"top_users":          []any{},
		"api_available":      false,
	}
	response, err := client.Do(request)
	if err == nil {
		defer response.Body.Close()
		data, readErr := io.ReadAll(io.LimitReader(response.Body, 2<<20))
		if readErr == nil && response.StatusCode >= 200 && response.StatusCode < 300 {
			var raw map[string]any
			if json.Unmarshal(data, &raw) == nil {
				connections, _ := raw["connections"].([]any)
				snapshot["active_connections"] = len(connections)
				snapshot["api_available"] = true
			}
		}
	}
	sequence, batchID := a.nextTelemetrySequence("connections")
	payload := map[string]any{"node_id": a.cfg.NodeID, "batch_id": batchID, "sequence": sequence, "snapshots": []any{snapshot}}
	if err := a.client.Connections(payload); err != nil {
		if spoolErr := a.enqueueSpool("connections", payload, 2); spoolErr != nil {
			a.log.Printf("connection telemetry dropped: %v", spoolErr)
		}
	}
}

// collectProxyConfig publishes the operator-facing projection of the local
// Clash API. The API is deliberately loopback-only; the control plane never
// reaches into a node or receives raw proxy endpoint credentials.
func (a *agent) collectProxyConfig() {
	if !a.proxyConfigMu.TryLock() {
		return
	}
	defer a.proxyConfigMu.Unlock()

	now := time.Now().UTC()
	interval := time.Duration(a.cfg.ProxyConfigIntervalSeconds) * time.Second
	if interval > 0 && !a.lastProxyConfigAt.IsZero() && now.Sub(a.lastProxyConfigAt) < interval {
		return
	}
	a.lastProxyConfigAt = now

	groups, err := a.readProxyGroupsAt(now)
	// The control-plane contract expects an array even when the local API is
	// unavailable. A nil slice would marshal as JSON null and be rejected
	// before the monitor can record the availability failure.
	groups = proxyConfigGroups(groups)
	payload := map[string]any{
		"node_id":       a.cfg.NodeID,
		"batch_id":      "",
		"sequence":      0,
		"sampled_at":    now,
		"api_available": err == nil,
		"groups":        groups,
		"error":         proxyConfigError(err),
	}
	sequence, batchID := a.nextTelemetrySequence("proxy_config")
	payload["sequence"] = sequence
	payload["batch_id"] = batchID
	if sendErr := a.client.ProxyConfig(payload); sendErr != nil {
		if spoolErr := a.enqueueSpool("proxy_config", payload, 2); spoolErr != nil {
			a.log.Printf("proxy config telemetry dropped: %v", spoolErr)
		}
	}
}

func proxyConfigGroups(groups []any) []any {
	if groups == nil {
		return []any{}
	}
	return groups
}

func (a *agent) readProxyGroups() ([]any, error) {
	return a.readProxyGroupsAt(time.Now().UTC())
}

func (a *agent) readProxyGroupsAt(now time.Time) ([]any, error) {
	var response struct {
		Proxies map[string]json.RawMessage `json:"proxies"`
	}
	proxyErr := a.clashAPIRequest(http.MethodGet, "/proxies", nil, &response)
	if proxyErr != nil {
		// Older Clash-compatible APIs may expose only the selector resource.
		// A successful fallback is still safe because it goes through the same
		// metadata-only projection and never returns endpoint credentials.
		fallback, fallbackErr := a.subscriptionSelectorProjection(nil)
		if fallbackErr == nil && len(fallback) > 0 {
			return fallback, nil
		}
		return nil, proxyErr
	}
	if response.Proxies == nil {
		fallback, fallbackErr := a.subscriptionSelectorProjection(nil)
		if fallbackErr == nil && len(fallback) > 0 {
			return fallback, nil
		}
		return nil, errors.New("clash_api_invalid_response")
	}

	keys := make([]string, 0, len(response.Proxies))
	metadata := make(map[string]map[string]any, len(response.Proxies))
	for key, raw := range response.Proxies {
		var value map[string]any
		if json.Unmarshal(raw, &value) != nil {
			continue
		}
		keys = append(keys, key)
		metadata[key] = value
	}
	a.collectProxyDelays(metadata, now)
	sort.Strings(keys)
	groups := make([]any, 0, len(keys))
	hasSubscriptionSelector := false
	for _, key := range keys {
		value := metadata[key]
		all := safeProxyNames(stringSlice(value["all"]))
		if len(all) == 0 {
			// A few Clash-compatible APIs call this field outbounds.
			all = safeProxyNames(stringSlice(value["outbounds"]))
		}
		if len(all) == 0 {
			continue
		}
		projected := proxyGroupProjection(key, value, all, metadata)
		if strings.EqualFold(stringValue(projected["name"]), "subscription") {
			hasSubscriptionSelector = true
		}
		groups = append(groups, projected)
	}

	// Always project the Grouproxy selector when it is available. Other Clash
	// groups are useful telemetry, but only this selector maps to a desired
	// bundle choice and should therefore be visible to the operator as a real
	// outbound service list.
	if !hasSubscriptionSelector {
		fallback, selectorErr := a.subscriptionSelectorProjection(metadata)
		if selectorErr == nil && len(fallback) > 0 {
			groups = append(groups, fallback...)
			hasSubscriptionSelector = true
		}
	}
	if len(groups) == 0 && len(response.Proxies) > 0 && len(metadata) == 0 {
		// A non-empty but undecodable response is malformed. A valid API
		// response containing only direct/block entries remains an online,
		// non-selectable view.
		return nil, errors.New("clash_api_invalid_response")
	}
	return groups, nil
}

type proxyDelayResult struct {
	name  string
	delay int
	err   error
}

func (a *agent) collectProxyDelays(metadata map[string]map[string]any, now time.Time) {
	interval := time.Duration(a.cfg.ProxyDelayIntervalSeconds) * time.Second
	if interval <= 0 || (!a.lastProxyDelayAt.IsZero() && now.Sub(a.lastProxyDelayAt) < interval) {
		return
	}
	a.lastProxyDelayAt = now
	targets := proxyDelayTargets(metadata)
	if len(targets) == 0 {
		return
	}

	jobs := make(chan string)
	results := make(chan proxyDelayResult, len(targets))
	workers := minInt(len(targets), proxyDelayConcurrency)
	var workersDone sync.WaitGroup
	workersDone.Add(workers)
	for range workers {
		go func() {
			defer workersDone.Done()
			for name := range jobs {
				delay, err := a.proxyDelay(name)
				results <- proxyDelayResult{name: name, delay: delay, err: err}
			}
		}()
	}
	for _, name := range targets {
		jobs <- name
	}
	close(jobs)
	workersDone.Wait()
	close(results)

	for result := range results {
		endpoint := metadata[result.name]
		if endpoint == nil {
			continue
		}
		if result.err != nil {
			// A per-outbound /delay failure means this particular path could
			// not reach the probe target. Keep any earlier latency evidence,
			// but make the current failure visible instead of looking pending.
			endpoint["alive"] = false
			continue
		}
		recordProxyDelay(endpoint, result.delay, now)
	}
}

func proxyDelayTargets(metadata map[string]map[string]any) []string {
	candidates := make(map[string]struct{})
	for _, value := range metadata {
		for _, name := range append(stringSlice(value["all"]), stringSlice(value["outbounds"])...) {
			endpoint := metadata[name]
			if endpoint == nil || !isDelayProbeable(endpoint) {
				continue
			}
			candidates[name] = struct{}{}
		}
	}
	targets := make([]string, 0, minInt(len(candidates), maxProxyDelayTargets))
	for name := range candidates {
		targets = append(targets, name)
	}
	sort.Strings(targets)
	if len(targets) > maxProxyDelayTargets {
		return targets[:maxProxyDelayTargets]
	}
	return targets
}

func isDelayProbeable(value map[string]any) bool {
	switch strings.ToLower(strings.ReplaceAll(safeProxyType(stringValue(value["type"])), "-", "")) {
	case "", "direct", "reject", "block", "selector", "urltest", "fallback", "loadbalance", "relay", "dns":
		return false
	default:
		return true
	}
}

func recordProxyDelay(value map[string]any, delay int, at time.Time) {
	if delay < 0 || delay > 300000 {
		return
	}
	history, _ := value["history"].([]any)
	history = append(history, map[string]any{
		"delay": delay,
		"time":  at.UTC().Format(time.RFC3339Nano),
	})
	if len(history) > 20 {
		history = history[len(history)-20:]
	}
	value["delay"] = delay
	value["history"] = history
}

func (a *agent) subscriptionSelectorProjection(metadata map[string]map[string]any) ([]any, error) {
	selector, err := a.readSubscriptionSelector()
	if err != nil {
		return nil, err
	}
	all := safeProxyNames(selector.All)
	if len(all) == 0 {
		return nil, nil
	}
	value := map[string]any{
		"type": selector.Type,
		"name": selector.Name,
		"now":  selector.Now,
		"all":  all,
		"udp":  selector.UDP,
	}
	return []any{proxyGroupProjection("subscription", value, all, metadata)}, nil
}

func proxyGroupProjection(key string, value map[string]any, all []string, metadata map[string]map[string]any) map[string]any {
	name := safeProxyLabel(stringValue(value["name"]))
	if name == "" {
		name = safeProxyLabel(key)
	}
	group := map[string]any{
		"name":     name,
		"type":     safeProxyType(stringValue(value["type"])),
		"now":      safeProxyLabel(stringValue(value["now"])),
		"all":      all,
		"nodes":    make([]any, 0, len(all)),
		"udp":      boolValue(value["udp"]),
		"delay_ms": latestProxyDelay(value),
		"history":  proxyHistoryProjection(value["history"]),
	}
	if group["type"] == "" {
		group["type"] = "unknown"
	}
	nodes := group["nodes"].([]any)
	for _, endpointName := range all {
		endpoint := metadata[endpointName]
		if endpoint == nil {
			endpoint = map[string]any{}
		}
		nodes = append(nodes, proxyEndpointProjection(endpointName, endpoint))
	}
	group["nodes"] = nodes
	return group
}

func proxyEndpointProjection(name string, value map[string]any) map[string]any {
	result := map[string]any{
		"name":     safeProxyLabel(name),
		"type":     safeProxyType(stringValue(value["type"])),
		"udp":      boolValue(value["udp"]),
		"delay_ms": latestProxyDelay(value),
		"history":  proxyHistoryProjection(value["history"]),
	}
	if result["type"] == "" {
		result["type"] = "unknown"
	}
	if alive, ok := value["alive"].(bool); ok {
		result["alive"] = alive
	}
	return result
}

func proxyHistoryProjection(value any) []any {
	items, ok := value.([]any)
	if !ok {
		return []any{}
	}
	result := make([]any, 0, minInt(len(items), 20))
	for _, raw := range items {
		entry, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		delay, valid := asInt(entry["delay"])
		if !valid || delay < 0 || delay > 300000 {
			continue
		}
		point := map[string]any{"delay_ms": delay}
		at := stringValue(entry["time"])
		if parsed, err := time.Parse(time.RFC3339Nano, at); err == nil {
			point["at"] = parsed.UTC()
		} else {
			point["at"] = nil
		}
		result = append(result, point)
		if len(result) >= 20 {
			break
		}
	}
	return result
}

func latestProxyDelay(value map[string]any) any {
	if delay, ok := asInt(value["delay"]); ok && delay >= 0 && delay <= 300000 {
		return delay
	}
	history := proxyHistoryProjection(value["history"])
	if len(history) > 0 {
		if point, ok := history[len(history)-1].(map[string]any); ok {
			return point["delay_ms"]
		}
	}
	return nil
}

func safeProxyNames(values []string) []string {
	result := make([]string, 0, minInt(len(values), 500))
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		name := safeProxyLabel(value)
		if name == "" {
			continue
		}
		if _, exists := seen[name]; exists {
			continue
		}
		seen[name] = struct{}{}
		result = append(result, name)
		if len(result) >= 500 {
			break
		}
	}
	return result
}

func safeProxyLabel(value string) string {
	value = strings.Join(strings.Fields(value), " ")
	runes := []rune(value)
	if len(runes) > 255 {
		return string(runes[:255])
	}
	return value
}

func safeProxyType(value string) string {
	value = safeProxyLabel(value)
	runes := []rune(value)
	if len(runes) > 64 {
		return string(runes[:64])
	}
	return value
}

func proxyConfigError(err error) string {
	if err == nil {
		return ""
	}
	if strings.Contains(err.Error(), "clash_api_invalid_response") {
		return "clash_api_invalid_response"
	}
	return "clash_api_unavailable"
}

func (a *agent) clashAPIRequest(method, path string, body any, out any) error {
	return a.clashAPIRequestWithTimeout(method, path, body, out, 800*time.Millisecond)
}

func (a *agent) clashAPIRequestWithTimeout(method, path string, body any, out any, timeout time.Duration) error {
	endpoint := "http://" + a.cfg.ClashAPIListen + path
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, endpoint, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	client := &http.Client{Timeout: timeout}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if readErr != nil {
		return readErr
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("clash_api_http_%d", resp.StatusCode)
	}
	if out != nil && len(data) > 0 {
		if err := json.Unmarshal(data, out); err != nil {
			return err
		}
	}
	return nil
}

func (a *agent) proxyDelay(name string) (int, error) {
	query := url.Values{}
	query.Set("url", proxyDelayTargetURL)
	query.Set("timeout", strconv.Itoa(proxyDelayTimeoutMilliseconds))
	var response struct {
		Delay int `json:"delay"`
	}
	err := a.clashAPIRequestWithTimeout(
		http.MethodGet,
		"/proxies/"+url.PathEscape(name)+"/delay?"+query.Encode(),
		nil,
		&response,
		time.Duration(proxyDelayTimeoutMilliseconds+1_000)*time.Millisecond,
	)
	if err != nil {
		return 0, err
	}
	if response.Delay < 0 || response.Delay > 300000 {
		return 0, errors.New("clash_api_invalid_delay")
	}
	return response.Delay, nil
}

func (a *agent) readSubscriptionSelector() (clashSelector, error) {
	var selector clashSelector
	err := a.clashAPIRequest(
		http.MethodGet,
		"/proxies/"+url.PathEscape("subscription"),
		nil,
		&selector,
	)
	return selector, err
}

func (a *agent) configuredProbeTags() []string {
	paths := []string{a.cfg.SingboxConfig, filepath.Join(a.cfg.StateDir, "last-good.json")}
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var configValue map[string]any
		if json.Unmarshal(data, &configValue) != nil {
			continue
		}
		outbounds, ok := configValue["outbounds"].([]any)
		if !ok {
			continue
		}
		for _, raw := range outbounds {
			entry, ok := raw.(map[string]any)
			if !ok || stringValue(entry["type"]) != "selector" || stringValue(entry["tag"]) != "subscription" {
				continue
			}
			return uniqueProbeTags(stringSlice(entry["outbounds"]), a.cfg.ProbeMaxOutbounds)
		}
	}
	return nil
}

func uniqueProbeTags(values []string, max int) []string {
	seen := make(map[string]struct{}, len(values))
	result := make([]string, 0, minInt(len(values), max))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" || value == "subscription" || value == "direct" || value == "block" {
			continue
		}
		if _, exists := seen[value]; exists {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
		if len(result) >= max {
			break
		}
	}
	return result
}

func minInt(left, right int) int {
	if left < right {
		return left
	}
	return right
}

func (a *agent) selectOutbound(tag string) error {
	return a.clashAPIRequest(
		http.MethodPut,
		"/proxies/"+url.PathEscape("subscription"),
		map[string]string{"name": tag},
		nil,
	)
}

func (a *agent) probeThroughProxy(targetURL string) (bool, string, int64) {
	started := time.Now()
	success := false
	errorClass := ""
	proxyURL, err := a.probeProxyURL()
	if err != nil {
		return false, "proxy_config_unavailable", time.Since(started).Milliseconds()
	}
	transport := &http.Transport{Proxy: http.ProxyURL(proxyURL), DisableKeepAlives: true}
	httpClient := &http.Client{
		Transport: transport,
		Timeout:   8 * time.Second,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	defer transport.CloseIdleConnections()
	req, err := http.NewRequest(http.MethodHead, targetURL, nil)
	if err != nil {
		return false, "request_error", time.Since(started).Milliseconds()
	}
	req.Header.Set("Accept", "*/*")
	resp, err := httpClient.Do(req)
	if err != nil {
		return false, "connect_error", time.Since(started).Milliseconds()
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusProxyAuthRequired {
		errorClass = "proxy_auth_required"
	} else if resp.StatusCode >= 500 {
		errorClass = fmt.Sprintf("http_%d", resp.StatusCode)
	} else {
		success = true
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1))
	return success, errorClass, time.Since(started).Milliseconds()
}

// probeProxyURL reads only the local HTTP inbound credentials that sing-box is
// currently using. This lets monitor-originated checks exercise the same
// authenticated proxy path as users without sending credentials anywhere.
func (a *agent) probeProxyURL() (*url.URL, error) {
	proxyURL, err := url.Parse(fmt.Sprintf("http://127.0.0.1:%d", a.cfg.ListenPort))
	if err != nil {
		return nil, err
	}
	configData, err := os.ReadFile(a.cfg.SingboxConfig)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return proxyURL, nil
		}
		return nil, err
	}
	var configValue struct {
		Inbounds []struct {
			Type  string `json:"type"`
			Tag   string `json:"tag"`
			Users []struct {
				Username string `json:"username"`
				Password string `json:"password"`
			} `json:"users"`
		} `json:"inbounds"`
	}
	if err := json.Unmarshal(configData, &configValue); err != nil {
		return nil, err
	}
	for _, inbound := range configValue.Inbounds {
		if inbound.Type != "http" || (inbound.Tag != "" && inbound.Tag != "grouproxy-http") {
			continue
		}
		if len(inbound.Users) > 0 && inbound.Users[0].Username != "" && inbound.Users[0].Password != "" {
			proxyURL.User = url.UserPassword(inbound.Users[0].Username, inbound.Users[0].Password)
		}
		break
	}
	return proxyURL, nil
}

func (a *agent) executeProbe(request client.ProbeRequest) {
	// Switching a selector is a process-wide operation. Serialize probe runs
	// and restore the operator's original selection before returning.
	a.probeMu.Lock()
	defer a.probeMu.Unlock()

	selector, selectorErr := a.readSubscriptionSelector()
	available := selector.All
	if len(available) == 0 {
		available = a.configuredProbeTags()
	}
	tags := uniqueProbeTags(request.OutboundTags, a.cfg.ProbeMaxOutbounds)
	if len(tags) == 0 {
		tags = uniqueProbeTags(available, a.cfg.ProbeMaxOutbounds)
	}
	if len(tags) == 0 {
		tags = []string{"subscription"}
	}
	availableSet := make(map[string]struct{}, len(available))
	for _, tag := range available {
		availableSet[tag] = struct{}{}
	}
	original := selector.Now
	current := original
	changed := false
	if selectorErr == nil && original != "" {
		defer func() {
			if changed && current != original {
				if err := a.selectOutbound(original); err != nil {
					a.log.Printf("restore selector %q: %v", original, err)
				}
			}
		}()
	}

	results := make([]map[string]any, 0, len(tags))
	for _, tag := range tags {
		if selectorErr != nil {
			results = append(results, probeResult(request.TargetURL, tag, false, "selector_unavailable", 0))
			continue
		}
		if _, exists := availableSet[tag]; !exists {
			results = append(results, probeResult(request.TargetURL, tag, false, "outbound_not_found", 0))
			continue
		}
		if tag != current {
			if err := a.selectOutbound(tag); err != nil {
				results = append(results, probeResult(request.TargetURL, tag, false, "selector_switch_error", 0))
				continue
			}
			current = tag
			changed = true
		}
		success, errorClass, latency := a.probeThroughProxy(request.TargetURL)
		results = append(results, probeResult(request.TargetURL, tag, success, errorClass, latency))
	}
	sequence, batchID := a.nextTelemetrySequence("probes")
	payload := map[string]any{
		"node_id":  a.cfg.NodeID,
		"batch_id": batchID,
		"sequence": sequence,
		"task_id":  request.TaskID,
		"results":  results,
	}
	if err := a.client.Probes(payload); err != nil {
		if spoolErr := a.enqueueSpool("probes", payload, 1); spoolErr != nil {
			a.log.Printf("probe telemetry dropped: %v", spoolErr)
		}
	}
}

func probeResult(targetURL, tag string, success bool, errorClass string, latency int64) map[string]any {
	return map[string]any{
		"outbound_tag": tag,
		"target_url":   targetURL,
		"success":      success,
		"latency_ms":   latency,
		"error_class":  errorClass,
		"sampled_at":   time.Now().UTC(),
	}
}

func (a *agent) sync(ctx context.Context) error {
	a.stateMu.Lock()
	appliedVersion, appliedHash := a.state.AppliedVersion, a.state.AppliedHash
	a.stateMu.Unlock()
	desired, err := a.client.Desired(a.cfg.NodeID, appliedVersion, appliedHash)
	if err != nil {
		a.setSyncError(err.Error())
		return err
	}
	// A reachable control plane resolves only the transient communication error.
	// state.LastError remains until a candidate bundle succeeds, so a failed
	// application is still visible as config_status=failed after reconnection.
	a.setSyncError("")
	if !desired.DesiredStale || desired.Bundle == nil {
		return nil
	}
	return a.applyBundle(ctx, desired.Bundle)
}

func (a *agent) applyBundle(ctx context.Context, value map[string]any) error {
	a.stateMu.Lock()
	current := a.state
	a.stateMu.Unlock()
	version, ok := asInt(value["desired_version"])
	if !ok {
		return a.ackFailure(value, "invalid_desired_version", "desired_version is not an integer", false, false, false, false)
	}
	if version < current.AppliedVersion || (version == current.AppliedVersion && stringValue(value["bundle_hash"]) != current.AppliedHash) {
		return a.ackFailure(value, "bundle_replay", "bundle version/hash is older than applied state", false, false, false, false)
	}
	hashValue, err := bundle.Validate(bundle.Bundle(value), a.cfg.HMACSecret, current.AppliedVersion)
	if err != nil {
		return a.ackFailure(value, errorCode(err), err.Error(), false, false, false, false)
	}
	if stringValue(value["node_id"]) != a.cfg.NodeID {
		return a.ackFailure(value, "bundle_node_mismatch", "bundle node_id does not match this monitor", false, false, false, false)
	}
	if err := bundle.ValidateMinimumMonitorVersion(bundle.Bundle(value), monitorVersion); err != nil {
		return a.ackFailure(value, errorCode(err), err.Error(), false, false, false, false)
	}
	listen, ok := value["listen"].(map[string]any)
	if !ok {
		return a.ackFailure(value, "invalid_listen", "listen object is missing", false, false, false, false)
	}
	port, ok := asInt(listen["http_port"])
	if !ok {
		return a.ackFailure(value, "invalid_http_port", "http_port is invalid", false, false, false, false)
	}
	if a.cfg.ListenPortOverride > 0 {
		port = a.cfg.ListenPortOverride
	}
	firewallPort := port
	if a.cfg.FirewallPortOverride > 0 {
		firewallPort = a.cfg.FirewallPortOverride
	}
	subscriptionOutbounds, subscriptionVersion, subscriptionHash, subscriptionStatus, err := a.resolveSubscription(value)
	if err != nil {
		return a.ackFailure(value, errorCode(err), err.Error(), false, false, false, false)
	}
	if err := validateProxySelection(value, subscriptionOutboundTags(subscriptionOutbounds)); err != nil {
		return a.ackFailure(value, errorCode(err), err.Error(), false, false, false, false)
	}
	if err := routingdata.Ensure(a.cfg.StateDir); err != nil {
		return a.ackFailure(value, "routing_data_write_failed", err.Error(), false, false, false, false)
	}
	configValue := renderSingbox(
		value,
		port,
		a.cfg.StateDir,
		a.cfg.ClashAPIListen,
		subscriptionOutbounds,
		a.cfg.ListenAddressOverride,
	)
	versionsDir := filepath.Join(a.cfg.StateDir, "versions")
	if err := os.MkdirAll(versionsDir, 0o700); err != nil {
		return err
	}
	candidatePath := filepath.Join(versionsDir, fmt.Sprintf("%d-%s.json", version, hashValue[:12]))
	if err := bundle.WriteJSON(candidatePath, configValue); err != nil {
		return err
	}
	if err := a.runtime.Check(candidatePath); err != nil {
		return a.ackFailure(value, "singbox_check_failed", err.Error(), true, false, false, false)
	}
	cidrs := stringSlice(value["allow_cidrs"])
	nftScript := firewall.Render(firewallPort, cidrs, boolValue(value["shutdown"]))
	if err := firewall.Check(nftScript); err != nil {
		return a.ackFailure(value, "nft_check_failed", err.Error(), true, false, false, false)
	}
	if err := bundle.WriteBytes(filepath.Join(a.cfg.StateDir, "candidate.nft"), []byte(nftScript), ".candidate-nft-*"); err != nil {
		return err
	}
	if a.cfg.FirewallMode == "apply" {
		if err := firewall.Apply(nftScript); err != nil {
			_ = a.restoreLastGoodFirewall()
			return a.ackFailure(value, "nft_apply_failed", err.Error(), true, false, false, false)
		}
	}
	a.runtime.ListenPort = port
	processOK, portOK, apiOK := false, false, false
	if ok, applyErr := a.runtime.Apply(candidatePath); !ok || applyErr != nil {
		message := "sing-box apply failed"
		if applyErr != nil {
			message = applyErr.Error()
		}
		_ = a.restoreLastGoodFirewall()
		return a.ackFailure(value, "singbox_apply_failed", message, true, false, false, true)
	}
	processOK, portOK, apiOK = a.runtime.Health(ctx)
	healthOK := processOK && portOK && apiOK
	if healthOK {
		for elapsed := 0; elapsed < a.cfg.HealthWindowSeconds; elapsed += a.cfg.HealthSampleSeconds {
			time.Sleep(time.Duration(a.cfg.HealthSampleSeconds) * time.Second)
			processOK, portOK, apiOK = a.runtime.Health(ctx)
			if !processOK || !portOK || !apiOK {
				healthOK = false
				break
			}
		}
	}
	if !healthOK {
		return a.rollback(value, candidatePath, "health_window_failed", processOK, portOK, apiOK)
	}
	if err := bundle.WriteJSON(a.cfg.SingboxConfig, configValue); err != nil {
		return err
	}
	if err := bundle.WriteJSON(filepath.Join(a.cfg.StateDir, "last-good.json"), configValue); err != nil {
		return err
	}
	if err := bundle.WriteJSON(filepath.Join(a.cfg.StateDir, "last-good-bundle.json"), value); err != nil {
		return err
	}
	if err := bundle.WriteBytes(filepath.Join(a.cfg.StateDir, "last-good-nft.json"), []byte(nftScript), ".last-good-nft-*"); err != nil {
		return err
	}
	a.stateMu.Lock()
	a.state.LastGoodBundle = value
	a.state.LastGoodVersion = version
	a.state.LastGoodHash = hashValue
	a.state.AppliedVersion = version
	a.state.AppliedHash = hashValue
	a.state.SubscriptionVersion = subscriptionVersion
	a.state.SubscriptionHash = subscriptionHash
	a.state.SubscriptionStatus = subscriptionStatus
	a.state.ConfigStatus = "in_sync"
	a.state.ServiceStatus = "healthy"
	a.state.LastError = ""
	a.state.LastReloadAt = time.Now().UTC()
	a.sequence++
	a.state.Sequence = a.sequence
	saveErr := state.Save(a.cfg.StateDir, a.state)
	a.stateMu.Unlock()
	if saveErr != nil {
		return saveErr
	}
	return a.sendAck(value, true, true, true, true, false, true, "succeeded", "", "")
}

func (a *agent) rollback(value map[string]any, candidatePath, reason string, processOK, portOK, apiOK bool) error {
	a.log.Printf("rolling back candidate %s: %s", candidatePath, reason)
	a.runtime.Close()
	rollbackOK := false
	a.stateMu.Lock()
	lastGood := a.state.LastGoodBundle
	a.stateMu.Unlock()
	nftRollbackOK := a.restoreLastGoodFirewall() == nil
	if lastGood != nil {
		if listen, ok := lastGood["listen"].(map[string]any); ok {
			if port, ok := asInt(listen["http_port"]); ok {
				if a.cfg.ListenPortOverride > 0 {
					port = a.cfg.ListenPortOverride
				}
				lastPath, err := a.ensureLastGoodConfig(lastGood, port)
				if err == nil {
					// Keep the configured path and the rollback snapshot identical;
					// a restart must not resurrect the failed candidate.
					if configData, readErr := os.ReadFile(lastPath); readErr != nil {
						a.log.Printf("read rollback config: %v", readErr)
					} else if err := bundle.WriteBytes(a.cfg.SingboxConfig, configData, ".rollback-*"); err != nil {
						a.log.Printf("write rollback config: %v", err)
					}
					a.runtime.ListenPort = port
					if ok, err := a.runtime.Apply(lastPath); ok && err == nil {
						oldProcessOK, oldPortOK, oldAPIOK := a.runtime.Health(context.Background())
						rollbackOK = oldProcessOK && oldPortOK && oldAPIOK && nftRollbackOK
					}
				}
			}
		}
	}
	a.stateMu.Lock()
	a.state.ConfigStatus = "failed"
	if !rollbackOK {
		a.state.ConfigStatus = "rollback_failed"
	}
	a.state.ServiceStatus = "healthy"
	if !rollbackOK {
		a.state.ServiceStatus = "unhealthy"
	}
	a.state.LastError = reason
	a.sequence++
	a.state.Sequence = a.sequence
	_ = state.Save(a.cfg.StateDir, a.state)
	a.stateMu.Unlock()
	_ = a.sendAck(value, false, processOK, nftRollbackOK, false, true, rollbackOK, "rolled_back", "health_check_failed", reason)
	return errors.New(reason)
}

func (a *agent) restoreLastGoodFirewall() error {
	a.stateMu.Lock()
	lastGood := a.state.LastGoodBundle
	a.stateMu.Unlock()
	if lastGood == nil {
		data, err := os.ReadFile(filepath.Join(a.cfg.StateDir, "last-good-bundle.json"))
		if err == nil {
			var persisted map[string]any
			if json.Unmarshal(data, &persisted) == nil {
				lastGood = persisted
			}
		}
	}
	if lastGood != nil {
		return a.restoreLastGoodFirewallForBundle(lastGood)
	}

	// Legacy fallback for state written before bundles were persisted locally.
	if a.cfg.FirewallMode != "apply" {
		return nil
	}
	path := filepath.Join(a.cfg.StateDir, "last-good-nft.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return firewall.Apply(string(data))
}

func (a *agent) restoreLastGoodFirewallForBundle(lastGood map[string]any) error {
	listen, ok := lastGood["listen"].(map[string]any)
	if !ok {
		return errors.New("last-good listen missing")
	}
	port, ok := asInt(listen["http_port"])
	if !ok {
		return errors.New("last-good port invalid")
	}
	script := firewall.Render(a.firewallPort(port), stringSlice(lastGood["allow_cidrs"]), boolValue(lastGood["shutdown"]))
	if err := firewall.Check(script); err != nil {
		return err
	}
	if err := bundle.WriteBytes(filepath.Join(a.cfg.StateDir, "last-good-nft.json"), []byte(script), ".last-good-nft-*"); err != nil {
		return err
	}
	if a.cfg.FirewallMode == "apply" {
		return firewall.Apply(script)
	}
	return nil
}

func (a *agent) restoreLastGood() error {
	a.stateMu.Lock()
	lastGood := a.state.LastGoodBundle
	a.stateMu.Unlock()
	if lastGood == nil {
		data, err := os.ReadFile(filepath.Join(a.cfg.StateDir, "last-good-bundle.json"))
		if err == nil {
			var persisted map[string]any
			if json.Unmarshal(data, &persisted) == nil {
				lastGood = persisted
				a.stateMu.Lock()
				a.state.LastGoodBundle = persisted
				a.stateMu.Unlock()
			}
		}
	}
	if lastGood == nil {
		return nil
	}
	listen, ok := lastGood["listen"].(map[string]any)
	if !ok {
		return errors.New("last-good listen missing")
	}
	port, ok := asInt(listen["http_port"])
	if !ok {
		return errors.New("last-good port invalid")
	}
	if a.cfg.ListenPortOverride > 0 {
		port = a.cfg.ListenPortOverride
	}
	path, err := a.ensureLastGoodConfig(lastGood, port)
	if err != nil {
		return err
	}
	configData, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err := bundle.WriteBytes(a.cfg.SingboxConfig, configData, ".restore-*"); err != nil {
		return err
	}
	a.runtime.ListenPort = port
	if err := a.runtime.Check(path); err != nil {
		return err
	}
	if ok, err := a.runtime.Apply(path); !ok || err != nil {
		return fmt.Errorf("restore last-good: %v", err)
	}
	if err := a.restoreLastGoodFirewallForBundle(lastGood); err != nil {
		return fmt.Errorf("restore last-good firewall: %w", err)
	}
	return nil
}

// ensureLastGoodConfig keeps the resolved configuration locally. In
// particular, restart and rollback do not need the control plane to retrieve
// a historical blob while it is unavailable.
func (a *agent) ensureLastGoodConfig(lastGood map[string]any, port int) (string, error) {
	if err := routingdata.Ensure(a.cfg.StateDir); err != nil {
		return "", err
	}
	path := filepath.Join(a.cfg.StateDir, "last-good.json")
	if data, err := os.ReadFile(path); err == nil {
		var configValue map[string]any
		if json.Unmarshal(data, &configValue) == nil {
			ensureRoutingRules(configValue, a.cfg.StateDir)
			applySingboxIngress(configValue, port, boolValue(lastGood["shutdown"]), a.cfg.ListenAddressOverride)
			if err := bundle.WriteJSON(path, configValue); err != nil {
				return "", err
			}
			return path, nil
		}
	}
	outbounds, _, _, _, err := a.resolveSubscription(lastGood)
	if err != nil {
		return "", err
	}
	if err := validateProxySelection(lastGood, subscriptionOutboundTags(outbounds)); err != nil {
		return "", err
	}
	configValue := renderSingbox(lastGood, port, a.cfg.StateDir, a.cfg.ClashAPIListen, outbounds, a.cfg.ListenAddressOverride)
	if err := bundle.WriteJSON(path, configValue); err != nil {
		return "", err
	}
	return path, nil
}

func (a *agent) ackFailure(value map[string]any, code, message string, singboxOK, nftOK, healthOK, rollbackAttempted bool) error {
	a.stateMu.Lock()
	a.state.ConfigStatus = "failed"
	a.state.LastError = message
	a.sequence++
	a.state.Sequence = a.sequence
	_ = state.Save(a.cfg.StateDir, a.state)
	a.stateMu.Unlock()
	_ = a.sendAck(value, false, singboxOK, nftOK, healthOK, rollbackAttempted, false, "failed", code, message)
	return fmt.Errorf("%s: %s", code, message)
}

func (a *agent) sendAck(value map[string]any, ok, singboxOK, nftOK, healthOK, rollbackAttempted, rollbackOK bool, stage, code, message string) error {
	version, _ := asInt(value["desired_version"])
	a.stateMu.Lock()
	sequence := a.sequence
	lastGood := a.state.LastGoodVersion
	appliedVersion := a.state.AppliedVersion
	appliedHash := a.state.AppliedHash
	a.stateMu.Unlock()
	payload := map[string]any{
		"node_id":            a.cfg.NodeID,
		"release_id":         stringValue(value["release_id"]),
		"desired_version":    version,
		"applied_version":    appliedVersion,
		"bundle_hash":        stringValue(value["bundle_hash"]),
		"applied_hash":       appliedHash,
		"ok":                 ok,
		"singbox_ok":         singboxOK,
		"nft_ok":             nftOK,
		"health_ok":          healthOK,
		"rollback_attempted": rollbackAttempted,
		"rollback_ok":        rollbackOK,
		"last_good_version":  lastGood,
		"stage":              stage,
		"error_code":         code,
		"error_message":      message,
		"sequence":           sequence,
	}
	return a.client.Ack(payload)
}

func (a *agent) sendHeartbeat(ctx context.Context) error {
	_ = ctx
	a.stateMu.Lock()
	a.sequence++
	a.state.Sequence = a.sequence
	if err := state.Save(a.cfg.StateDir, a.state); err != nil {
		a.stateMu.Unlock()
		return err
	}
	value := a.state
	sequence := a.sequence
	syncError := a.syncError
	a.stateMu.Unlock()
	processOK, portOK, apiOK := a.runtime.Health(context.Background())
	status, lastError := heartbeatStatus(value, syncError, processOK, portOK)
	payload := map[string]any{
		"node_id":             a.cfg.NodeID,
		"monitor_version":     monitorVersion,
		"singbox_version":     singboxVersion(a.cfg.SingboxBin),
		"desired_version":     value.AppliedVersion,
		"applied_version":     value.AppliedVersion,
		"applied_hash":        value.AppliedHash,
		"bundle_hash":         value.LastGoodHash,
		"liveness_status":     status,
		"config_status":       value.ConfigStatus,
		"service_status":      value.ServiceStatus,
		"subscription_status": subscriptionStatus(value.SubscriptionStatus),
		"process_ok":          processOK,
		"port_ok":             portOK,
		"api_ok":              apiOK,
		"spool_bytes":         directorySize(filepath.Join(a.cfg.StateDir, "spool")),
		"last_error":          lastError,
		"sequence":            sequence,
	}
	response, err := a.client.Heartbeat(payload)
	if err != nil {
		// Collection still advances its local offset and persists failed
		// batches in spool, so a control-plane outage does not lose deny data.
		a.sendTelemetry(ctx)
		return err
	}
	for _, probeRequest := range response.ProbeRequests {
		go a.executeProbe(probeRequest)
	}
	a.sendTelemetry(ctx)
	return nil
}

func (a *agent) setSyncError(message string) {
	a.stateMu.Lock()
	a.syncError = message
	a.stateMu.Unlock()
}

// heartbeatStatus keeps liveness separate from configuration state. A monitor
// with a rejected candidate can remain online and keep serving last-good.
func heartbeatStatus(value state.State, syncError string, processOK, portOK bool) (string, string) {
	status := "online"
	if !processOK || !portOK || syncError != "" {
		status = "degraded"
	}
	lastError := value.LastError
	if lastError == "" {
		lastError = syncError
	}
	return status, lastError
}

func (a *agent) resolveSubscription(value map[string]any) ([]any, int, string, string, error) {
	raw, exists := value["subscription"]
	if !exists || raw == nil {
		return nil, 0, "", "not_configured", nil
	}
	spec, ok := raw.(map[string]any)
	if !ok {
		return nil, 0, "", "", errors.New("invalid_subscription")
	}
	version, ok := asInt(spec["version"])
	if !ok || version < 1 {
		return nil, 0, "", "", errors.New("invalid_subscription_version")
	}
	hashValue := stringValue(spec["hash"])
	format := stringValue(spec["format"])
	var content []byte
	if inline, present := spec["content"].(string); present {
		content = []byte(inline)
	} else if _, present := spec["blob_url"].(string); present {
		var err error
		content, err = a.client.Blob(hashValue)
		if err != nil {
			return nil, 0, "", "", fmt.Errorf("subscription_blob_fetch_failed: %w", err)
		}
	} else {
		return nil, 0, "", "", errors.New("invalid_subscription_content")
	}
	if err := subscription.VerifyHash(content, hashValue); err != nil {
		return nil, 0, "", "", err
	}
	outbounds, err := subscription.Parse(content, format)
	if err != nil {
		return nil, 0, "", "", err
	}
	return outbounds, version, hashValue, "current", nil
}

func subscriptionStatus(value string) string {
	if value == "" {
		return "not_configured"
	}
	return value
}

func localRuleSets(stateDir string) []any {
	return []any{
		map[string]any{
			"type":   "local",
			"tag":    routingdata.GeoIPCNTag,
			"format": "binary",
			"path":   routingdata.Path(stateDir, routingdata.GeoIPCNTag),
		},
		map[string]any{
			"type":   "local",
			"tag":    routingdata.GeoSiteCNTag,
			"format": "binary",
			"path":   routingdata.Path(stateDir, routingdata.GeoSiteCNTag),
		},
	}
}

func cnDirectRule() map[string]any {
	return map[string]any{
		"rule_set": []string{routingdata.GeoIPCNTag, routingdata.GeoSiteCNTag},
		"outbound": "direct",
	}
}

// ensureRoutingRules upgrades a persisted pre-rule-set last-good config in
// place. It deliberately uses the already resolved config, so no historical
// subscription blob needs to be fetched while the control plane is down.
func ensureRoutingRules(configValue map[string]any, stateDir string) bool {
	route, ok := configValue["route"].(map[string]any)
	if !ok {
		return false
	}
	rules, ok := route["rules"].([]any)
	if !ok {
		return false
	}
	changed := false
	if !hasCNRuleSetDefinitions(route["rule_set"]) {
		route["rule_set"] = localRuleSets(stateDir)
		changed = true
	}
	if !hasCNDirectRule(rules) {
		route["rules"] = append(rules, cnDirectRule())
		changed = true
	}
	return changed
}

func hasCNRuleSetDefinitions(value any) bool {
	definitions, ok := value.([]any)
	if !ok {
		return false
	}
	foundIP, foundSite := false, false
	for _, raw := range definitions {
		definition, ok := raw.(map[string]any)
		if !ok || stringValue(definition["type"]) != "local" || stringValue(definition["format"]) != "binary" {
			continue
		}
		switch stringValue(definition["tag"]) {
		case routingdata.GeoIPCNTag:
			foundIP = true
		case routingdata.GeoSiteCNTag:
			foundSite = true
		}
	}
	return foundIP && foundSite
}

func hasCNDirectRule(rules []any) bool {
	for _, raw := range rules {
		rule, ok := raw.(map[string]any)
		if !ok || stringValue(rule["outbound"]) != "direct" {
			continue
		}
		tags := stringSlice(rule["rule_set"])
		if len(tags) == 2 && tags[0] == routingdata.GeoIPCNTag && tags[1] == routingdata.GeoSiteCNTag {
			return true
		}
	}
	return false
}

func (a *agent) firewallPort(listenPort int) int {
	if a.cfg.FirewallPortOverride > 0 {
		return a.cfg.FirewallPortOverride
	}
	return listenPort
}

func applySingboxIngress(configValue map[string]any, port int, shutdown bool, listenAddress string) {
	inbounds, ok := configValue["inbounds"].([]any)
	if !ok || len(inbounds) == 0 {
		return
	}
	inbound, ok := inbounds[0].(map[string]any)
	if !ok {
		return
	}
	if listenAddress == "" {
		listenAddress = "0.0.0.0"
	}
	if shutdown {
		listenAddress = "127.0.0.1"
	}
	inbound["listen"] = listenAddress
	inbound["listen_port"] = port
	delete(inbound, "proxy_protocol")
	delete(inbound, "proxy_protocol_accept_no_header")
}

func renderSingbox(
	value map[string]any,
	port int,
	stateDir string,
	clashAPIListen string,
	subscriptionOutbounds []any,
	listenAddress string,
) map[string]any {
	inbound := map[string]any{
		"type":             "http",
		"tag":              "grouproxy-http",
		"listen_port":      port,
		"set_system_proxy": false,
	}
	if users := proxyAuthUsers(value); len(users) > 0 {
		inbound["users"] = users
	}
	configValue := map[string]any{"inbounds": []any{inbound}}
	applySingboxIngress(configValue, port, boolValue(value["shutdown"]), listenAddress)
	routeRules := make([]any, 0)
	allowCIDRs := stringSlice(value["allow_cidrs"])
	if len(allowCIDRs) == 0 {
		// An empty effective ACL is fail-closed at the sing-box layer too.
		routeRules = append(routeRules, map[string]any{"action": "reject"})
	} else {
		// source_ip_cidr is a route matcher in sing-box.  Inverting the
		// allow-list rejects every source that nftables would reject as well.
		routeRules = append(routeRules, map[string]any{
			"source_ip_cidr": allowCIDRs,
			"invert":         true,
			"action":         "reject",
		})
	}
	if deny, ok := value["deny_destinations"].([]any); ok {
		for _, raw := range deny {
			entry, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			pattern := stringValue(entry["pattern"])
			kind := stringValue(entry["kind"])
			if pattern == "" {
				continue
			}
			rule := map[string]any{"outbound": "block"}
			switch kind {
			case "ip", "cidr":
				rule["ip_cidr"] = []string{pattern}
			default:
				rule["domain"] = []string{pattern}
			}
			routeRules = append(routeRules, rule)
		}
	}
	// Pin CN domain and IP handling to local binary rule-sets. Explicit
	// blacklist rules remain above this rule, so destination deny policy wins.
	routeRules = append(routeRules, cnDirectRule())
	outbounds := []any{
		map[string]any{"type": "direct", "tag": "direct"},
		map[string]any{"type": "block", "tag": "block"},
	}
	finalOutbound := "direct"
	if len(subscriptionOutbounds) > 0 {
		tags := make([]string, 0, len(subscriptionOutbounds))
		for _, raw := range subscriptionOutbounds {
			entry, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			tag := stringValue(entry["tag"])
			if tag == "" {
				continue
			}
			tags = append(tags, tag)
			outbounds = append(outbounds, entry)
		}
		if len(tags) > 0 {
			outbounds = append(outbounds, map[string]any{
				"type":      "selector",
				"tag":       "subscription",
				"outbounds": tags,
				"default":   selectedSubscriptionOutbound(value, tags),
			})
			finalOutbound = "subscription"
		}
	}
	return map[string]any{
		"log":       map[string]any{"level": "info", "output": filepath.Join(stateDir, "sing-box.log")},
		"inbounds":  []any{inbound},
		"outbounds": outbounds,
		"route": map[string]any{
			"rule_set": localRuleSets(stateDir),
			"rules":    routeRules,
			"final":    finalOutbound,
		},
		"experimental": map[string]any{
			"clash_api": map[string]any{
				"external_controller": clashAPIListen,
			},
		},
	}
}

func subscriptionOutboundTags(outbounds []any) []string {
	tags := make([]string, 0, len(outbounds))
	seen := make(map[string]struct{}, len(outbounds))
	for _, raw := range outbounds {
		entry, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		tag := strings.TrimSpace(stringValue(entry["tag"]))
		if tag == "" {
			continue
		}
		if _, exists := seen[tag]; exists {
			continue
		}
		seen[tag] = struct{}{}
		tags = append(tags, tag)
	}
	return tags
}

func validateProxySelection(value map[string]any, tags []string) error {
	raw, exists := value["proxy_selection"]
	if !exists || raw == nil {
		return nil
	}
	selection, ok := raw.(map[string]any)
	if !ok {
		return errors.New("invalid_proxy_selection")
	}
	if strings.TrimSpace(stringValue(selection["group"])) != "subscription" {
		return errors.New("proxy_group_not_selectable")
	}
	wanted := strings.TrimSpace(stringValue(selection["outbound"]))
	if wanted == "" {
		return errors.New("proxy_outbound_not_found")
	}
	for _, tag := range tags {
		if tag == wanted {
			return nil
		}
	}
	return errors.New("proxy_outbound_not_found")
}

// selectedSubscriptionOutbound converts the control-plane's safe selector
// choice into the selector default. applyBundle validates a requested choice
// against the current parsed subscription before this renderer is reached.
func selectedSubscriptionOutbound(value map[string]any, tags []string) string {
	if len(tags) == 0 {
		return ""
	}
	raw, ok := value["proxy_selection"].(map[string]any)
	if !ok {
		return tags[0]
	}
	if strings.TrimSpace(stringValue(raw["group"])) != "subscription" {
		return tags[0]
	}
	wanted := strings.TrimSpace(stringValue(raw["outbound"]))
	for _, tag := range tags {
		if tag == wanted {
			return tag
		}
	}
	return tags[0]
}

func proxyAuthUsers(value map[string]any) []any {
	rawAuth, ok := value["proxy_auth"].(map[string]any)
	if !ok || !boolValue(rawAuth["required"]) {
		return nil
	}
	rawUsers, ok := rawAuth["users"].([]any)
	if !ok {
		return nil
	}
	users := make([]any, 0, len(rawUsers))
	for _, rawUser := range rawUsers {
		user, ok := rawUser.(map[string]any)
		if !ok {
			continue
		}
		username, usernameOK := user["username"].(string)
		password, passwordOK := user["password"].(string)
		if !usernameOK || !passwordOK || username == "" || password == "" {
			continue
		}
		// Only keep the sing-box HTTP inbound fields; the signed bundle is
		// still validated earlier, but config rendering should never carry
		// through incidental object keys.
		users = append(users, map[string]any{"username": username, "password": password})
	}
	return users
}

func singboxVersion(binary string) string {
	cmd := exec.Command(binary, "version")
	data, err := cmd.Output()
	if err != nil {
		return "unknown"
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "sing-box version ") {
			return strings.TrimSpace(strings.TrimPrefix(line, "sing-box version "))
		}
	}
	return "unknown"
}

func directorySize(path string) int64 {
	var total int64
	_ = filepath.Walk(path, func(_ string, info os.FileInfo, err error) error {
		if err == nil && info != nil && !info.IsDir() {
			total += info.Size()
		}
		return nil
	})
	return total
}

func asInt(value any) (int, bool) {
	switch typed := value.(type) {
	case int:
		return typed, true
	case float64:
		return int(typed), typed == float64(int(typed))
	case json.Number:
		parsed, err := strconv.Atoi(string(typed))
		return parsed, err == nil
	default:
		return 0, false
	}
}

func stringValue(value any) string {
	if value == nil {
		return ""
	}
	return fmt.Sprint(value)
}

func boolValue(value any) bool {
	result, _ := value.(bool)
	return result
}

func stringSlice(value any) []string {
	switch items := value.(type) {
	case []any:
		result := make([]string, 0, len(items))
		for _, item := range items {
			result = append(result, stringValue(item))
		}
		return result
	case []string:
		return append([]string(nil), items...)
	default:
		return nil
	}
}

func errorCode(err error) string {
	if err == nil {
		return "unknown"
	}
	return strings.ReplaceAll(err.Error(), " ", "_")
}

// Keep net/http linked in the first static build as a smoke check for the
// standard transport used by the client package.
var _ = http.MethodGet
