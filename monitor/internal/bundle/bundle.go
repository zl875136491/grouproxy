package bundle

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type Bundle map[string]any

const proxyListenPort = 1080

func canonical(value any) ([]byte, error) {
	// Keep escaping aligned with Python's ensure_ascii=False. encoding/json
	// orders map keys, giving both sides deterministic bytes.
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

func without(value Bundle, keys ...string) Bundle {
	result := make(Bundle, len(value))
	for key, item := range value {
		remove := false
		for _, excluded := range keys {
			if key == excluded {
				remove = true
				break
			}
		}
		if !remove {
			result[key] = item
		}
	}
	return result
}

func stringValue(value any) string {
	if value == nil {
		return ""
	}
	return fmt.Sprint(value)
}

func intValue(value any) (int, bool) {
	switch typed := value.(type) {
	case int:
		return typed, true
	case int64:
		return int(typed), true
	case float64:
		return int(typed), typed == float64(int(typed))
	case json.Number:
		parsed, err := strconv.Atoi(string(typed))
		return parsed, err == nil
	default:
		return 0, false
	}
}

func Hash(value Bundle) (string, error) {
	data, err := canonical(without(value, "bundle_hash", "mac"))
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(data)
	return fmt.Sprintf("%x", digest[:]), nil
}

func Verify(value Bundle, secret string) (string, error) {
	hashValue, err := Hash(value)
	if err != nil {
		return "", err
	}
	if !hmac.Equal([]byte(hashValue), []byte(stringValue(value["bundle_hash"]))) {
		return "", errors.New("bundle_hash_mismatch")
	}
	unsigned, err := canonical(without(value, "mac"))
	if err != nil {
		return "", err
	}
	macValue := hmac.New(sha256.New, []byte(secret))
	_, _ = macValue.Write(unsigned)
	expected := fmt.Sprintf("%x", macValue.Sum(nil))
	if !hmac.Equal([]byte(expected), []byte(stringValue(value["mac"]))) {
		return "", errors.New("bundle_mac_mismatch")
	}
	return hashValue, nil
}

func Validate(value Bundle, secret string, currentVersion int) (string, error) {
	if value == nil {
		return "", errors.New("empty_bundle")
	}
	if schema, exists := intValue(value["schema_version"]); !exists || schema != 1 {
		return "", errors.New("unsupported_schema_version")
	}
	if stringValue(value["release_id"]) == "" || stringValue(value["site_id"]) == "" || stringValue(value["node_id"]) == "" {
		return "", errors.New("missing_identity")
	}
	version, ok := intValue(value["desired_version"])
	if !ok || version <= 0 {
		return "", errors.New("invalid_desired_version")
	}
	if version < currentVersion {
		return "", errors.New("bundle_replay")
	}
	if version == currentVersion && stringValue(value["bundle_hash"]) == "" {
		return "", errors.New("duplicate_version_without_hash")
	}
	expires := stringValue(value["expires_at"])
	if expires == "" {
		return "", errors.New("missing_expiry")
	}
	when, err := time.Parse(time.RFC3339, expires)
	if err != nil || !when.After(time.Now().UTC()) {
		return "", errors.New("bundle_expired")
	}
	listen, ok := value["listen"].(map[string]any)
	if !ok {
		return "", errors.New("invalid_listen")
	}
	port, ok := intValue(listen["http_port"])
	if !ok || port < 1 || port > 65535 {
		return "", errors.New("invalid_http_port")
	}
	if port != proxyListenPort {
		return "", errors.New("unsupported_http_port")
	}
	if err := ValidateSourceBlacklist(value); err != nil {
		return "", err
	}
	if err := validateSubscription(value); err != nil {
		return "", err
	}
	if _, err := Verify(value, secret); err != nil {
		return "", err
	}
	return stringValue(value["bundle_hash"]), nil
}

func rejectRetiredPolicyFields(value Bundle) error {
	for _, field := range []string{"allow_cidrs", "deny_destinations", "deny_sources"} {
		if _, exists := value[field]; exists {
			return errors.New("retired_policy_field")
		}
	}
	return nil
}

// ValidateSourceBlacklist verifies the only source-policy representation the
// monitor accepts from the control plane: a canonical flat JSON array. It is
// also used when normalizing local last-good state before recovery.
func ValidateSourceBlacklist(value Bundle) error {
	if err := rejectRetiredPolicyFields(value); err != nil {
		return err
	}
	raw, exists := value["source_blacklist"]
	if !exists || raw == nil {
		return errors.New("missing_source_blacklist")
	}
	return validateSourceBlacklistEntries(raw, stringValue(value["site_id"]))
}

func validateSourceBlacklistEntries(raw any, bundleSiteID string) error {
	var entries []any
	switch typed := raw.(type) {
	case []any:
		entries = typed
	default:
		return errors.New("invalid_source_blacklist_entries")
	}
	if len(entries) > 10_000 {
		return errors.New("source_blacklist_too_large")
	}
	for _, rawEntry := range entries {
		entry, ok := rawEntry.(map[string]any)
		if !ok {
			return errors.New("invalid_source_blacklist_entry")
		}
		kind, kindOK := entry["kind"].(string)
		pattern, patternOK := entry["pattern"].(string)
		if !kindOK || !patternOK || pattern == "" || pattern != strings.TrimSpace(pattern) || len(pattern) > 512 {
			return errors.New("invalid_source_blacklist_pattern")
		}
		switch kind {
		case "ip":
			if !isCanonicalSourceIP(pattern) {
				return errors.New("invalid_source_blacklist_ip")
			}
		case "network":
			if !isCanonicalSourceNetwork(pattern) {
				return errors.New("invalid_source_blacklist_network")
			}
		case "domain":
			if !isCanonicalSourceDomain(pattern) {
				return errors.New("invalid_source_blacklist_domain")
			}
		default:
			return errors.New("invalid_source_blacklist_kind")
		}
		scope, scopeOK := entry["scope"].(string)
		if !scopeOK || (scope != "global" && scope != "site") {
			return errors.New("invalid_source_blacklist_scope")
		}
		entrySiteID := ""
		if rawSiteID, exists := entry["site_id"]; exists && rawSiteID != nil {
			var siteIDOK bool
			entrySiteID, siteIDOK = rawSiteID.(string)
			if !siteIDOK || entrySiteID != strings.TrimSpace(entrySiteID) {
				return errors.New("invalid_source_blacklist_site")
			}
		}
		if scope == "global" && entrySiteID != "" {
			return errors.New("invalid_source_blacklist_site")
		}
		if scope == "site" && (entrySiteID == "" || entrySiteID != bundleSiteID) {
			return errors.New("invalid_source_blacklist_site")
		}
	}
	return nil
}

func isCanonicalSourceIP(pattern string) bool {
	address, err := netip.ParseAddr(pattern)
	return err == nil && address.String() == pattern
}

func isCanonicalSourceNetwork(pattern string) bool {
	prefix, err := netip.ParsePrefix(pattern)
	return err == nil && prefix.Masked() == prefix && prefix.String() == pattern
}

func isCanonicalSourceDomain(pattern string) bool {
	if pattern == "" || len(pattern) > 253 || pattern != strings.ToLower(pattern) || strings.HasSuffix(pattern, ".") {
		return false
	}
	if _, err := netip.ParseAddr(pattern); err == nil {
		return false
	}
	labels := strings.Split(pattern, ".")
	allNumericAddressLabels := true
	for _, label := range labels {
		if !isCanonicalHostnameLabel(label) {
			return false
		}
		allNumericAddressLabels = allNumericAddressLabels && isNumericAddressLabel(label)
	}
	return !allNumericAddressLabels
}

func isCanonicalHostnameLabel(label string) bool {
	if len(label) == 0 || len(label) > 63 {
		return false
	}
	for index := 0; index < len(label); index++ {
		character := label[index]
		if character > 0x7f || (character < 'a' || character > 'z') && (character < '0' || character > '9') && character != '-' {
			return false
		}
		if (index == 0 || index == len(label)-1) && character == '-' {
			return false
		}
	}
	return true
}

func isNumericAddressLabel(label string) bool {
	if strings.HasPrefix(label, "0x") && len(label) > 2 {
		for _, character := range label[2:] {
			if !(character >= '0' && character <= '9') && !(character >= 'a' && character <= 'f') {
				return false
			}
		}
		return true
	}
	for _, character := range label {
		if character < '0' || character > '9' {
			return false
		}
	}
	return label != ""
}

func validateSubscription(value Bundle) error {
	raw, exists := value["subscription"]
	if !exists || raw == nil {
		return nil
	}
	subscription, ok := raw.(map[string]any)
	if !ok {
		return errors.New("invalid_subscription")
	}
	hash := stringValue(subscription["hash"])
	if len(hash) != sha256.Size*2 {
		return errors.New("invalid_subscription_hash")
	}
	if _, err := hex.DecodeString(hash); err != nil {
		return errors.New("invalid_subscription_hash")
	}
	format := stringValue(subscription["format"])
	if format != "clash" && format != "sip008" && format != "sing-box" {
		return errors.New("invalid_subscription_format")
	}
	version, validVersion := intValue(subscription["version"])
	if !validVersion || version < 1 {
		return errors.New("invalid_subscription_version")
	}
	content, hasContent := subscription["content"].(string)
	blobURL, hasBlobURL := subscription["blob_url"].(string)
	if hasContent && len(content) > 10<<20 {
		return errors.New("subscription_content_too_large")
	}
	if (hasContent && hasBlobURL) || (!hasContent && !hasBlobURL) || (hasBlobURL && blobURL == "") {
		return errors.New("invalid_subscription_content")
	}
	return nil
}

// ValidateMinimumMonitorVersion prevents an old monitor from silently
// interpreting a newer bundle contract. Phase 0/1 uses strict x.y.z versions;
// later versions can replace this with a full semver parser if prereleases are
// introduced into the release channel.
func ValidateMinimumMonitorVersion(value Bundle, current string) error {
	required := stringValue(value["min_monitor_version"])
	if required == "" {
		return errors.New("missing_min_monitor_version")
	}
	requiredParts, err := versionParts(required)
	if err != nil {
		return errors.New("invalid_min_monitor_version")
	}
	currentParts, err := versionParts(current)
	if err != nil {
		return errors.New("invalid_monitor_version")
	}
	for index := range requiredParts {
		if currentParts[index] > requiredParts[index] {
			return nil
		}
		if currentParts[index] < requiredParts[index] {
			return errors.New("monitor_version_too_old")
		}
	}
	return nil
}

func versionParts(value string) ([3]int, error) {
	var result [3]int
	parts := strings.Split(strings.TrimPrefix(strings.TrimSpace(value), "v"), ".")
	if len(parts) != len(result) {
		return result, errors.New("invalid_version")
	}
	for index, part := range parts {
		parsed, err := strconv.Atoi(part)
		if err != nil || parsed < 0 {
			return result, errors.New("invalid_version")
		}
		result[index] = parsed
	}
	return result, nil
}

func WriteJSON(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return WriteBytes(path, data, ".candidate-*")
}

// WriteBytes atomically replaces a state/configuration file and keeps the
// file private to the monitor account.
func WriteBytes(path string, data []byte, pattern string) error {
	if err := os.MkdirAll(filepathDir(path), 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepathDir(path), pattern)
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}

func filepathDir(path string) string {
	return filepath.Dir(path)
}
