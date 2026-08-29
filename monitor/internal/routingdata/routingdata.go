// Package routingdata provides the pinned local rule-sets used by every
// monitor-rendered sing-box configuration.  The monitor never asks a node to
// download routing data at runtime.
package routingdata

import (
	"bytes"
	"crypto/sha256"
	_ "embed"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

const (
	GeoIPCNTag   = "geoip-cn"
	GeoSiteCNTag = "geosite-cn"
)

//go:embed assets/geoip-cn.srs
var geoIPCN []byte

//go:embed assets/geosite-cn.srs
var geoSiteCN []byte

type asset struct {
	tag      string
	filename string
	sha256   string
	content  []byte
}

var assets = []asset{
	{
		tag:      GeoIPCNTag,
		filename: "geoip-cn.srs",
		sha256:   "0acf5dad38fba9db2dade29ce5e4edc6902220944f30628ae46ed16cb0ec5edd",
		content:  geoIPCN,
	},
	{
		tag:      GeoSiteCNTag,
		filename: "geosite-cn.srs",
		sha256:   "63f6ef9ca510efd74cfa7def8e1e093a781886558d8aad4760984fddb16811ef",
		content:  geoSiteCN,
	},
}

// Dir is private monitor state, so both the bundled assets and any resolved
// last-good configuration remain available without a control-plane request.
func Dir(stateDir string) string {
	return filepath.Join(stateDir, "rule-sets")
}

// Path returns the local binary rule-set path for a known tag.
func Path(stateDir, tag string) string {
	for _, item := range assets {
		if item.tag == tag {
			return filepath.Join(Dir(stateDir), item.filename)
		}
	}
	return ""
}

// Ensure atomically writes verified embedded assets when they are missing or
// stale. A local rule-set is used rather than a remote sing-box rule-set so a
// restart, rollback, or control-plane outage cannot change route behavior.
func Ensure(stateDir string) error {
	directory := Dir(stateDir)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return fmt.Errorf("create routing data directory: %w", err)
	}
	for _, item := range assets {
		if err := ensureAsset(directory, item); err != nil {
			return err
		}
	}
	return nil
}

func ensureAsset(directory string, item asset) error {
	expected, err := expectedHash(item)
	if err != nil {
		return err
	}
	path := filepath.Join(directory, item.filename)
	current, readErr := os.ReadFile(path)
	if readErr == nil && bytes.Equal(hash(current), expected) {
		return nil
	}
	if readErr != nil && !errors.Is(readErr, os.ErrNotExist) {
		return fmt.Errorf("read routing data %s: %w", item.filename, readErr)
	}

	temporary, err := os.CreateTemp(directory, ".routing-data-*")
	if err != nil {
		return fmt.Errorf("create routing data %s: %w", item.filename, err)
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(0o600); err != nil {
		_ = temporary.Close()
		return fmt.Errorf("protect routing data %s: %w", item.filename, err)
	}
	if _, err := temporary.Write(item.content); err != nil {
		_ = temporary.Close()
		return fmt.Errorf("write routing data %s: %w", item.filename, err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("close routing data %s: %w", item.filename, err)
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return fmt.Errorf("replace routing data %s: %w", item.filename, err)
	}
	return nil
}

func expectedHash(item asset) ([]byte, error) {
	expected := fmt.Sprintf("%x", hash(item.content))
	if expected != item.sha256 {
		return nil, fmt.Errorf("embedded routing data hash mismatch for %s", item.filename)
	}
	return hash(item.content), nil
}

func hash(content []byte) []byte {
	digest := sha256.Sum256(content)
	return digest[:]
}
