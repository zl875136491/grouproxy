package main

import (
	"encoding/json"
	"errors"
	"net/netip"
	"strings"
)

type destinationRule struct {
	Kind    string `json:"kind"`
	Pattern string `json:"pattern"`
	Action  string `json:"action"`
}

type destinationPolicy struct {
	Mode          string            `json:"mode"`
	PrivateDirect bool              `json:"private_direct"`
	CNDirect      bool              `json:"cn_direct"`
	Final         string            `json:"final"`
	Rules         []destinationRule `json:"rules"`
}

func parseDestinationPolicy(value map[string]any) (destinationPolicy, error) {
	policy := destinationPolicy{Mode: "rule", PrivateDirect: true, CNDirect: true, Final: "proxy"}
	raw, exists := value["routing_policy"]
	if !exists {
		return policy, nil
	}
	if raw == nil {
		return policy, errors.New("invalid_routing_policy")
	}
	data, err := json.Marshal(raw)
	if err != nil {
		return policy, errors.New("invalid_routing_policy")
	}
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&policy); err != nil {
		return policy, errors.New("invalid_routing_policy")
	}
	if policy.Mode != "rule" || (policy.Final != "proxy" && policy.Final != "direct") || len(policy.Rules) > 200 {
		return policy, errors.New("invalid_routing_policy")
	}
	for _, rule := range policy.Rules {
		if rule.Action != "direct" && rule.Action != "proxy" && rule.Action != "reject" {
			return policy, errors.New("invalid_routing_action")
		}
		switch rule.Kind {
		case "ip_cidr":
			if _, err := netip.ParsePrefix(rule.Pattern); err != nil {
				return policy, errors.New("invalid_routing_cidr")
			}
		case "domain", "domain_suffix":
			if len(rule.Pattern) > 253 || rule.Pattern != strings.ToLower(rule.Pattern) {
				return policy, errors.New("invalid_routing_domain")
			}
			if _, err := netip.ParseAddr(rule.Pattern); err == nil {
				return policy, errors.New("invalid_routing_domain")
			}
			for _, label := range strings.Split(rule.Pattern, ".") {
				if len(label) == 0 || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
					return policy, errors.New("invalid_routing_domain")
				}
				for _, character := range label {
					if !(character >= 'a' && character <= 'z' || character >= '0' && character <= '9' || character == '-') {
						return policy, errors.New("invalid_routing_domain")
					}
				}
			}
		default:
			return policy, errors.New("invalid_routing_kind")
		}
	}
	return policy, nil
}

func destinationRouteRules(policy destinationPolicy, proxyOutbound string) []any {
	rules := make([]any, 0, len(policy.Rules)+2)
	for _, rule := range policy.Rules {
		entry := map[string]any{rule.Kind: []string{rule.Pattern}}
		if rule.Action == "reject" {
			entry["action"] = "reject"
		} else if rule.Action == "proxy" {
			entry["outbound"] = proxyOutbound
		} else {
			entry["outbound"] = "direct"
		}
		rules = append(rules, entry)
	}
	if policy.PrivateDirect {
		rules = append(rules, map[string]any{"ip_is_private": true, "outbound": "direct"})
	}
	if policy.CNDirect {
		rules = append(rules, cnDirectRule())
	}
	return rules
}

func validateDestinationOutbounds(value map[string]any, outbounds []any) error {
	policy, err := parseDestinationPolicy(value)
	if err != nil {
		return err
	}
	if _, configured := value["routing_policy"]; !configured || len(outbounds) > 0 {
		return nil
	}
	if policy.Final == "proxy" {
		return errors.New("routing_proxy_unavailable")
	}
	for _, rule := range policy.Rules {
		if rule.Action == "proxy" {
			return errors.New("routing_proxy_unavailable")
		}
	}
	return nil
}

func applyDestinationPolicy(configValue, value map[string]any, sourceRules []sourceBlacklistRule) error {
	policy, err := parseDestinationPolicy(value)
	if err != nil {
		return err
	}
	proxyOutbound := "direct"
	outbounds, _ := configValue["outbounds"].([]any)
	for _, raw := range outbounds {
		if entry, ok := raw.(map[string]any); ok && entry["tag"] == "subscription" {
			proxyOutbound = "subscription"
		}
	}
	route, _ := configValue["route"].(map[string]any)
	if route == nil {
		route = map[string]any{}
		configValue["route"] = route
	}
	route["rules"] = append(sourceBlacklistRouteRules(sourceRules), destinationRouteRules(policy, proxyOutbound)...)
	route["final"] = "direct"
	if policy.Final == "proxy" {
		route["final"] = proxyOutbound
	}
	experimental, _ := configValue["experimental"].(map[string]any)
	if experimental == nil {
		experimental = map[string]any{}
		configValue["experimental"] = experimental
	}
	clashAPI, _ := experimental["clash_api"].(map[string]any)
	if clashAPI == nil {
		clashAPI = map[string]any{}
		experimental["clash_api"] = clashAPI
	}
	clashAPI["default_mode"] = "rule"
	return nil
}
