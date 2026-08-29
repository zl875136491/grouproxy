package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
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

const monitorVersion = "0.2.1"

type agent struct {
	cfg       config.Config
	client    *client.Client
	state     state.State
	runtime   *runtime.Manager
	sequence  int
	log       *log.Logger
	stateMu   sync.Mutex
	lastError string
}

func main() {
	configPath := flag.String("config", "/etc/grouproxy/monitor.yaml", "monitor configuration")
	once := flag.Bool("once", false, "fetch and apply once, then exit")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("load config: %v", err)
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
				a.lastError = err.Error()
				a.log.Printf("sync failed: %v", err)
			}
		case <-heartbeatTicker.C:
			if err := a.sendHeartbeat(ctx); err != nil {
				a.log.Printf("heartbeat failed: %v", err)
			}
		}
	}
}

func (a *agent) sync(ctx context.Context) error {
	a.stateMu.Lock()
	appliedVersion, appliedHash := a.state.AppliedVersion, a.state.AppliedHash
	a.stateMu.Unlock()
	desired, err := a.client.Desired(a.cfg.NodeID, appliedVersion, appliedHash)
	if err != nil {
		return err
	}
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
	subscriptionOutbounds, subscriptionVersion, subscriptionHash, subscriptionStatus, err := a.resolveSubscription(value)
	if err != nil {
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
	nftScript := firewall.Render(port, cidrs, boolValue(value["shutdown"]))
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
	a.lastError = ""
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
	nftRollbackOK := a.restoreLastGoodFirewall() == nil
	a.stateMu.Lock()
	lastGood := a.state.LastGoodBundle
	a.stateMu.Unlock()
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
	if err := a.restoreLastGoodFirewall(); err != nil && a.cfg.FirewallMode == "apply" {
		return fmt.Errorf("restore last-good firewall: %v", err)
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
		if json.Unmarshal(data, &configValue) == nil && ensureRoutingRules(configValue, a.cfg.StateDir) {
			if err := bundle.WriteJSON(path, configValue); err != nil {
				return "", err
			}
			return path, nil
		}
		if configValue != nil {
			return path, nil
		}
	}
	outbounds, _, _, _, err := a.resolveSubscription(lastGood)
	if err != nil {
		return "", err
	}
	configValue := renderSingbox(lastGood, port, a.cfg.StateDir, a.cfg.ClashAPIListen, outbounds)
	if err := bundle.WriteJSON(path, configValue); err != nil {
		return "", err
	}
	return path, nil
}

func (a *agent) ackFailure(value map[string]any, code, message string, singboxOK, nftOK, healthOK, rollbackAttempted bool) error {
	a.stateMu.Lock()
	a.state.ConfigStatus = "failed"
	a.state.LastError = message
	a.lastError = message
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
	a.stateMu.Unlock()
	processOK, portOK, apiOK := a.runtime.Health(context.Background())
	status := "online"
	if !processOK || !portOK {
		status = "degraded"
	}
	if a.lastError != "" {
		status = "degraded"
	}
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
		"last_error":          a.lastError,
		"sequence":            sequence,
	}
	return a.client.Heartbeat(payload)
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

func renderSingbox(
	value map[string]any,
	port int,
	stateDir string,
	clashAPIListen string,
	subscriptionOutbounds []any,
) map[string]any {
	inbound := map[string]any{
		"type":             "http",
		"tag":              "grouproxy-http",
		"listen":           "0.0.0.0",
		"listen_port":      port,
		"set_system_proxy": false,
	}
	if boolValue(value["shutdown"]) {
		inbound["listen"] = "127.0.0.1"
	}
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
				"default":   tags[0],
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
