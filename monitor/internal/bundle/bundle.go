package bundle

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type Bundle map[string]any

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
	switch cidrs := value["allow_cidrs"].(type) {
	case []any:
		for _, raw := range cidrs {
			if _, _, err := net.ParseCIDR(stringValue(raw)); err != nil {
				return "", errors.New("invalid_allow_cidr")
			}
		}
	case []string:
		for _, raw := range cidrs {
			if _, _, err := net.ParseCIDR(raw); err != nil {
				return "", errors.New("invalid_allow_cidr")
			}
		}
	default:
		return "", errors.New("invalid_allow_cidr_list")
	}
	if err := validateProxyAuth(value); err != nil {
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

func validateProxyAuth(value Bundle) error {
	raw, exists := value["proxy_auth"]
	if !exists || raw == nil {
		return errors.New("missing_proxy_auth")
	}
	proxyAuth, ok := raw.(map[string]any)
	if !ok {
		return errors.New("invalid_proxy_auth")
	}
	required, ok := proxyAuth["required"].(bool)
	if !ok {
		return errors.New("invalid_proxy_auth_required")
	}
	rawUsers, exists := proxyAuth["users"]
	if !exists {
		return errors.New("missing_proxy_auth_users")
	}
	users, ok := rawUsers.([]any)
	if !ok {
		return errors.New("invalid_proxy_auth_users")
	}
	if len(users) > 10_000 {
		return errors.New("proxy_auth_too_many_users")
	}
	if required && len(users) == 0 {
		return errors.New("proxy_auth_requires_user")
	}
	if !required && len(users) != 0 {
		return errors.New("proxy_auth_users_not_allowed")
	}
	seen := make(map[string]struct{}, len(users))
	for _, rawUser := range users {
		user, ok := rawUser.(map[string]any)
		if !ok {
			return errors.New("invalid_proxy_auth_user")
		}
		username, usernameOK := user["username"].(string)
		password, passwordOK := user["password"].(string)
		if !usernameOK || !passwordOK || strings.TrimSpace(username) == "" || password == "" {
			return errors.New("invalid_proxy_auth_user")
		}
		if len(username) > 128 || len(password) > 512 {
			return errors.New("invalid_proxy_auth_user")
		}
		if _, duplicate := seen[username]; duplicate {
			return errors.New("duplicate_proxy_auth_username")
		}
		seen[username] = struct{}{}
	}
	return nil
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
