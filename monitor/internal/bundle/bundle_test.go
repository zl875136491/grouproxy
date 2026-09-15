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
		"listen":              map[string]any{"http_port": 1080},
		"blacklist":           []any{},
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

func TestValidateRejectsReplayAndRetiredPolicyFields(t *testing.T) {
	signed := signForTest(testBundle(), "secret")
	if _, err := Validate(signed, "secret", 2); err == nil || err.Error() != "bundle_replay" {
		t.Fatalf("replay error = %v", err)
	}
	for _, field := range []string{"allow_cidrs", "deny_destinations", "deny_sources"} {
		value := testBundle()
		value[field] = []any{}
		if _, err := Validate(signForTest(value, "secret"), "secret", 0); err == nil || err.Error() != "retired_policy_field" {
			t.Fatalf("%s error = %v", field, err)
		}
	}
}

func TestValidateAllowsCanonicalSourceBlacklist(t *testing.T) {
	value := testBundle()
	value["blacklist"] = []any{
		map[string]any{"direction": "source", "kind": "ip", "pattern": "192.0.2.1"},
		map[string]any{"direction": "source", "kind": "cidr", "pattern": "2001:db8::/32"},
		map[string]any{"direction": "destination", "kind": "domain", "pattern": "blocked.example"},
	}
	signed := signForTest(value, "secret")
	if _, err := Validate(signed, "secret", 0); err != nil {
		t.Fatalf("blacklist bundle rejected: %v", err)
	}
}

func TestValidateRejectsMalformedSourceBlacklist(t *testing.T) {
	value := testBundle()
	value["blacklist"] = []any{map[string]any{
		"direction": "source", "kind": "ip", "pattern": "not-an-ip",
	}}
	signed := signForTest(value, "secret")
	if _, err := Validate(signed, "secret", 0); err == nil || err.Error() != "invalid_blacklist_ip" {
		t.Fatalf("malformed blacklist error = %v", err)
	}
}

func TestValidateRejectsMissingOrNestedSourceBlacklist(t *testing.T) {
	missing := testBundle()
	delete(missing, "blacklist")
	if _, err := Validate(signForTest(missing, "secret"), "secret", 0); err == nil || err.Error() != "missing_blacklist" {
		t.Fatalf("missing blacklist error = %v", err)
	}

	nested := testBundle()
	nested["blacklist"] = map[string]any{
		"global": []any{map[string]any{"kind": "ip", "pattern": "192.0.2.1"}},
	}
	if _, err := Validate(signForTest(nested, "secret"), "secret", 0); err == nil || err.Error() != "invalid_blacklist_entries" {
		t.Fatalf("nested blacklist error = %v", err)
	}

	legacyNetworkKind := testBundle()
	legacyNetworkKind["blacklist"] = []any{map[string]any{
		"direction": "source", "kind": "network", "pattern": "192.0.2.0/24",
	}}
	if _, err := Validate(signForTest(legacyNetworkKind, "secret"), "secret", 0); err != nil {
		t.Fatalf("network alias should be accepted as cidr: %v", err)
	}

	typedSlice := testBundle()
	typedSlice["blacklist"] = []map[string]any{{
		"direction": "source", "kind": "ip", "pattern": "192.0.2.1",
	}}
	if _, err := Validate(signForTest(typedSlice, "secret"), "secret", 0); err == nil || err.Error() != "invalid_blacklist_entries" {
		t.Fatalf("non-JSON blacklist slice error = %v", err)
	}
}

func TestValidateRejectsNonCanonicalSourcePatternsAndWrongSite(t *testing.T) {
	testCases := []struct {
		name  string
		entry map[string]any
		err   string
	}{
		{
			name:  "noncanonical IPv6",
			entry: map[string]any{"direction": "source", "kind": "ip", "pattern": "2001:0DB8::1"},
			err:   "invalid_blacklist_ip",
		},
		{
			name:  "cidr with host bits",
			entry: map[string]any{"direction": "source", "kind": "cidr", "pattern": "192.0.2.7/24"},
			err:   "invalid_blacklist_cidr",
		},
		{
			name:  "noncanonical domain",
			entry: map[string]any{"direction": "destination", "kind": "domain", "pattern": "Blocked.Example."},
			err:   "invalid_blacklist_domain",
		},
		{
			name:  "URL domain",
			entry: map[string]any{"direction": "destination", "kind": "domain", "pattern": "https://blocked.example"},
			err:   "invalid_blacklist_domain",
		},
		{
			name:  "missing direction",
			entry: map[string]any{"kind": "ip", "pattern": "192.0.2.1"},
			err:   "invalid_blacklist_direction",
		},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			value := testBundle()
			value["blacklist"] = []any{testCase.entry}
			if _, err := Validate(signForTest(value, "secret"), "secret", 0); err == nil || err.Error() != testCase.err {
				t.Fatalf("error = %v, want %q", err, testCase.err)
			}
		})
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

func signForTest(value Bundle, secret string) Bundle {
	hashValue, _ := Hash(value)
	value["bundle_hash"] = hashValue
	unsigned, _ := canonical(without(value, "mac"))
	macValue := hmac.New(sha256.New, []byte(secret))
	_, _ = macValue.Write(unsigned)
	value["mac"] = fmt.Sprintf("%x", macValue.Sum(nil))
	return value
}
