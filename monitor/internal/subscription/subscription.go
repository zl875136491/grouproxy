// Package subscription validates the immutable payload embedded in a Desired
// Bundle and converts it to sing-box endpoint outbounds. It deliberately does
// not expose an editor or fetch arbitrary URLs from a node.
package subscription

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"gopkg.in/yaml.v3"
)

const (
	maxNodes       = 128
	maxFieldLength = 8192
)

var supportedTypes = map[string]bool{
	"anytls":      true,
	"http":        true,
	"hysteria2":   true,
	"shadowsocks": true,
	"socks":       true,
	"ssh":         true,
	"trojan":      true,
	"tuic":        true,
	"vless":       true,
	"vmess":       true,
	"wireguard":   true,
}

// VerifyHash ensures fetched content is the exact immutable version declared
// by the signed bundle before any parser runs.
func VerifyHash(content []byte, expected string) error {
	if len(expected) != sha256.Size*2 {
		return errors.New("subscription_hash_invalid")
	}
	if _, err := hex.DecodeString(expected); err != nil {
		return errors.New("subscription_hash_invalid")
	}
	digest := sha256.Sum256(content)
	if !strings.EqualFold(fmt.Sprintf("%x", digest[:]), expected) {
		return errors.New("subscription_hash_mismatch")
	}
	return nil
}

// Parse returns endpoint outbounds only. Direct/block/selector objects and
// route rules remain monitor-owned, so an upstream cannot replace local ACLs.
func Parse(content []byte, format string) ([]any, error) {
	switch format {
	case "sing-box":
		return parseSingbox(content)
	case "sip008":
		return parseSIP008(content)
	case "clash":
		return parseClash(content)
	default:
		return nil, errors.New("subscription_format_unsupported")
	}
}

func parseSingbox(content []byte) ([]any, error) {
	var raw any
	if err := json.Unmarshal(content, &raw); err != nil {
		return nil, errors.New("subscription_json_invalid")
	}
	var entries []any
	switch value := raw.(type) {
	case []any:
		entries = value
	case map[string]any:
		entries, _ = value["outbounds"].([]any)
	default:
		return nil, errors.New("subscription_json_invalid")
	}
	return validateOutbounds(entries)
}

func parseSIP008(content []byte) ([]any, error) {
	var document map[string]any
	if err := json.Unmarshal(content, &document); err != nil {
		return nil, errors.New("subscription_sip008_invalid")
	}
	servers, ok := document["servers"].([]any)
	if !ok || len(servers) == 0 || len(servers) > maxNodes {
		return nil, errors.New("subscription_sip008_invalid")
	}
	result := make([]any, 0, len(servers))
	for index, raw := range servers {
		server, ok := raw.(map[string]any)
		if !ok {
			return nil, errors.New("subscription_sip008_invalid")
		}
		host, err := requiredString(server["server"])
		if err != nil {
			return nil, errors.New("subscription_sip008_invalid")
		}
		port, ok := portValue(server["server_port"])
		if !ok {
			return nil, errors.New("subscription_sip008_invalid")
		}
		method, err := requiredString(server["method"])
		if err != nil {
			return nil, errors.New("subscription_sip008_invalid")
		}
		password, err := requiredString(server["password"])
		if err != nil {
			return nil, errors.New("subscription_sip008_invalid")
		}
		tag := fmt.Sprintf("subscription-%d", index+1)
		if name, err := optionalString(server["remarks"]); err == nil && name != "" {
			tag = name
		}
		result = append(result, map[string]any{
			"type":        "shadowsocks",
			"tag":         tag,
			"server":      host,
			"server_port": port,
			"method":      method,
			"password":    password,
		})
	}
	return validateOutbounds(result)
}

func parseClash(content []byte) ([]any, error) {
	var document map[string]any
	if err := yaml.Unmarshal(content, &document); err != nil {
		return nil, errors.New("subscription_clash_invalid")
	}
	rawProxies, ok := document["proxies"].([]any)
	if !ok || len(rawProxies) == 0 || len(rawProxies) > maxNodes {
		return nil, errors.New("subscription_clash_invalid")
	}
	result := make([]any, 0, len(rawProxies))
	for index, raw := range rawProxies {
		proxy, ok := raw.(map[string]any)
		if !ok {
			return nil, errors.New("subscription_clash_invalid")
		}
		kind, err := requiredString(proxy["type"])
		if err != nil {
			return nil, errors.New("subscription_clash_invalid")
		}
		kind = strings.ToLower(kind)
		if kind == "ss" {
			kind = "shadowsocks"
		}
		if kind != "shadowsocks" && kind != "trojan" && kind != "vmess" && kind != "vless" {
			return nil, errors.New("subscription_clash_type_unsupported")
		}
		host, err := requiredString(proxy["server"])
		if err != nil {
			return nil, errors.New("subscription_clash_invalid")
		}
		port, ok := portValue(proxy["port"])
		if !ok {
			return nil, errors.New("subscription_clash_invalid")
		}
		tag := fmt.Sprintf("subscription-%d", index+1)
		if name, err := optionalString(proxy["name"]); err == nil && name != "" {
			tag = name
		}
		outbound := map[string]any{
			"type":        kind,
			"tag":         tag,
			"server":      host,
			"server_port": port,
		}
		switch kind {
		case "shadowsocks":
			method, methodErr := requiredString(proxy["cipher"])
			password, passwordErr := requiredString(proxy["password"])
			if methodErr != nil || passwordErr != nil {
				return nil, errors.New("subscription_clash_invalid")
			}
			outbound["method"] = method
			outbound["password"] = password
		case "trojan":
			password, passwordErr := requiredString(proxy["password"])
			if passwordErr != nil {
				return nil, errors.New("subscription_clash_invalid")
			}
			outbound["password"] = password
		case "vmess", "vless":
			uuid, uuidErr := optionalString(proxy["uuid"])
			if uuidErr != nil || uuid == "" {
				uuid, uuidErr = optionalString(proxy["password"])
			}
			if uuidErr != nil || uuid == "" {
				return nil, errors.New("subscription_clash_invalid")
			}
			outbound["uuid"] = uuid
		}
		tls, tlsErr := clashTLS(proxy, kind, host)
		if tlsErr != nil {
			return nil, tlsErr
		}
		if tls != nil {
			outbound["tls"] = tls
		}
		// ``flow`` and ``packet_encoding`` already use sing-box's field names.
		// Transport is accepted only when the upstream has provided a sing-box
		// shaped object; route, selector and direct/block ownership always remain
		// with the monitor.
		for _, key := range []string{"flow", "packet_encoding", "transport"} {
			if value, exists := proxy[key]; exists {
				outbound[key] = value
			}
		}
		result = append(result, outbound)
	}
	return validateOutbounds(result)
}

// clashTLS converts the commonly used Clash TLS fields into the explicit
// sing-box TLS object. In particular, Trojan is always TLS-backed: omitting
// this conversion silently changes a valid Clash endpoint into a plain TCP
// connection and results in a timeout rather than a useful configuration
// failure.
func clashTLS(proxy map[string]any, kind, host string) (map[string]any, error) {
	tls := map[string]any{}
	enabled := false
	if raw, exists := proxy["tls"]; exists {
		switch value := raw.(type) {
		case bool:
			enabled = value
		case map[string]any:
			if rawEnabled, present := value["enabled"]; present {
				parsed, ok := rawEnabled.(bool)
				if !ok {
					return nil, errors.New("subscription_clash_tls_invalid")
				}
				enabled = parsed
			} else {
				enabled = true
			}
			copyTLSField(tls, value, "server_name")
			copyTLSField(tls, value, "insecure")
			copyTLSField(tls, value, "alpn")
			copyTLSField(tls, value, "utls")
		default:
			return nil, errors.New("subscription_clash_tls_invalid")
		}
	}

	// Trojan's protocol handshake always runs over TLS. Its documented
	// sing-box configuration uses an explicit TLS object even when the Clash
	// source omits a separate ``tls: true`` flag.
	if kind == "trojan" {
		enabled = true
	}

	for _, key := range []string{"sni", "servername", "server-name"} {
		if raw, exists := proxy[key]; exists {
			serverName, err := optionalString(raw)
			if err != nil {
				return nil, errors.New("subscription_clash_tls_invalid")
			}
			if serverName != "" {
				tls["server_name"] = serverName
				enabled = true
			}
			break
		}
	}
	if _, present := tls["server_name"]; !present && enabled && host != "" {
		tls["server_name"] = host
	}
	if raw, exists := proxy["skip-cert-verify"]; exists {
		insecure, ok := raw.(bool)
		if !ok {
			return nil, errors.New("subscription_clash_tls_invalid")
		}
		tls["insecure"] = insecure
		enabled = true
	}
	if raw, exists := proxy["alpn"]; exists {
		alpn, err := stringList(raw)
		if err != nil {
			return nil, errors.New("subscription_clash_tls_invalid")
		}
		if len(alpn) > 0 {
			tls["alpn"] = alpn
			enabled = true
		}
	}
	if raw, exists := proxy["client-fingerprint"]; exists {
		fingerprint, err := optionalString(raw)
		if err != nil || fingerprint == "" {
			return nil, errors.New("subscription_clash_tls_invalid")
		}
		tls["utls"] = map[string]any{"enabled": true, "fingerprint": fingerprint}
		enabled = true
	}
	if !enabled {
		return nil, nil
	}
	tls["enabled"] = true
	return tls, nil
}

func copyTLSField(destination, source map[string]any, key string) {
	if value, exists := source[key]; exists {
		destination[key] = value
	}
}

func stringList(value any) ([]string, error) {
	if text, ok := value.(string); ok {
		text = strings.TrimSpace(text)
		if text == "" {
			return nil, nil
		}
		return []string{text}, nil
	}
	items, ok := value.([]any)
	if !ok {
		return nil, errors.New("invalid_string_list")
	}
	result := make([]string, 0, len(items))
	for _, item := range items {
		text, err := optionalString(item)
		if err != nil || text == "" {
			return nil, errors.New("invalid_string_list")
		}
		result = append(result, text)
	}
	return result, nil
}

func validateOutbounds(entries []any) ([]any, error) {
	if len(entries) == 0 || len(entries) > maxNodes {
		return nil, errors.New("subscription_outbounds_invalid")
	}
	tags := map[string]bool{}
	result := make([]any, 0, len(entries))
	for index, raw := range entries {
		entry, ok := raw.(map[string]any)
		if !ok {
			return nil, errors.New("subscription_outbound_invalid")
		}
		kind, err := requiredString(entry["type"])
		if err != nil || !supportedTypes[strings.ToLower(kind)] {
			return nil, errors.New("subscription_outbound_type_unsupported")
		}
		tag, tagErr := optionalString(entry["tag"])
		if tagErr != nil {
			return nil, errors.New("subscription_tag_invalid")
		}
		if tag == "" {
			tag = fmt.Sprintf("subscription-%d", index+1)
			entry["tag"] = tag
		}
		if tag == "direct" || tag == "block" || tag == "subscription" || tags[tag] {
			return nil, errors.New("subscription_tag_invalid")
		}
		tags[tag] = true
		result = append(result, entry)
	}
	return result, nil
}

func requiredString(value any) (string, error) {
	result, err := optionalString(value)
	if err != nil || result == "" {
		return "", errors.New("invalid_string")
	}
	return result, nil
}

func optionalString(value any) (string, error) {
	if value == nil {
		return "", nil
	}
	result, ok := value.(string)
	if !ok || len(result) > maxFieldLength {
		return "", errors.New("invalid_string")
	}
	return strings.TrimSpace(result), nil
}

func portValue(value any) (int, bool) {
	switch typed := value.(type) {
	case int:
		return typed, typed > 0 && typed <= 65535
	case float64:
		port := int(typed)
		return port, typed == float64(port) && port > 0 && port <= 65535
	case json.Number:
		port, err := typed.Int64()
		return int(port), err == nil && port > 0 && port <= 65535
	default:
		return 0, false
	}
}
