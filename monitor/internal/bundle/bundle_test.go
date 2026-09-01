package bundle

import (
	"crypto/hmac"
	"crypto/sha256"
	"fmt"
	"testing"
	"time"
)

func testBundle() Bundle {
	return Bundle{
		"schema_version":      1,
		"release_id":          "release-1",
		"desired_version":     1,
		"min_monitor_version": "0.1.0",
		"site_id":             "site-1",
		"node_id":             "node-1",
		"listen":              map[string]any{"http_port": 80},
		"allow_cidrs":         []string{"10.0.0.0/8"},
		"deny_destinations":   []any{},
		"proxy_auth":          map[string]any{"required": false, "users": []any{}},
		"shutdown":            false,
		"issued_at":           time.Now().UTC().Format(time.RFC3339),
		"expires_at":          time.Now().UTC().Add(time.Hour).Format(time.RFC3339),
	}
}

func TestSignVerifyAndCanonicalHash(t *testing.T) {
	value := testBundle()
	signed := signForTest(value, "secret")
	hashValue, err := Verify(signed, "secret")
	if err != nil {
		t.Fatalf("Verify() error = %v", err)
	}
	if hashValue != stringValue(signed["bundle_hash"]) {
		t.Fatalf("hash = %q, bundle hash = %q", hashValue, signed["bundle_hash"])
	}
	if _, err := Verify(signed, "wrong"); err == nil {
		t.Fatal("Verify() accepted an invalid MAC")
	}
}

func TestValidateRejectsReplayAndMalformedACL(t *testing.T) {
	signed := signForTest(testBundle(), "secret")
	if _, err := Validate(signed, "secret", 2); err == nil || err.Error() != "bundle_replay" {
		t.Fatalf("replay error = %v", err)
	}
	signed["allow_cidrs"] = []string{"not-a-cidr"}
	if _, err := Validate(signed, "secret", 0); err == nil || err.Error() != "invalid_allow_cidr" {
		t.Fatalf("ACL error = %v", err)
	}
}

func TestValidateMinimumMonitorVersion(t *testing.T) {
	value := testBundle()
	if err := ValidateMinimumMonitorVersion(value, "0.1.0"); err != nil {
		t.Fatalf("matching monitor version rejected: %v", err)
	}
	if err := ValidateMinimumMonitorVersion(value, "0.0.9"); err == nil || err.Error() != "monitor_version_too_old" {
		t.Fatalf("old monitor error = %v", err)
	}
	value["min_monitor_version"] = "not-semver"
	if err := ValidateMinimumMonitorVersion(value, "0.1.0"); err == nil || err.Error() != "invalid_min_monitor_version" {
		t.Fatalf("invalid requirement error = %v", err)
	}
}

func TestValidateProxyAuth(t *testing.T) {
	value := testBundle()
	value["proxy_auth"] = map[string]any{
		"required": true,
		"users": []any{map[string]any{"username": "example.user", "password": "derived-secret"}},
	}
	signed := signForTest(value, "secret")
	if _, err := Validate(signed, "secret", 0); err != nil {
		t.Fatalf("valid proxy auth rejected: %v", err)
	}

	value = testBundle()
	value["proxy_auth"] = map[string]any{"required": true, "users": []any{}}
	signed = signForTest(value, "secret")
	if _, err := Validate(signed, "secret", 0); err == nil || err.Error() != "proxy_auth_requires_user" {
		t.Fatalf("empty required users error = %v", err)
	}

	value = testBundle()
	value["proxy_auth"] = map[string]any{
		"required": true,
		"users": []any{
			map[string]any{"username": "example.user", "password": "first"},
			map[string]any{"username": "example.user", "password": "second"},
		},
	}
	signed = signForTest(value, "secret")
	if _, err := Validate(signed, "secret", 0); err == nil || err.Error() != "duplicate_proxy_auth_username" {
		t.Fatalf("duplicate user error = %v", err)
	}
}

func signForTest(value Bundle, secret string) Bundle {
	hashValue, _ := Hash(value)
	value["bundle_hash"] = hashValue
	unsigned, _ := canonical(without(value, "mac"))
	macValue := hmac.New(sha256.New, []byte(secret))
	_, _ = macValue.Write(unsigned)
	value["mac"] = fmt.Sprintf("%x", macValue.Sum(nil))
	return value
}
