package state

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"
)

type State struct {
	LastGoodBundle      map[string]any `json:"last_good_bundle,omitempty"`
	LastGoodVersion     int            `json:"last_good_version"`
	LastGoodHash        string         `json:"last_good_hash"`
	AppliedVersion      int            `json:"applied_version"`
	AppliedHash         string         `json:"applied_hash"`
	SubscriptionVersion int            `json:"subscription_version"`
	SubscriptionHash    string         `json:"subscription_hash"`
	SubscriptionStatus  string         `json:"subscription_status"`
	Sequence            int            `json:"sequence"`
	ConfigStatus        string         `json:"config_status"`
	ServiceStatus       string         `json:"service_status"`
	LastError           string         `json:"last_error,omitempty"`
	LastReloadAt        time.Time      `json:"last_reload_at,omitempty"`
}

func Load(dir string) (State, error) {
	path := filepath.Join(dir, "monitor-state.json")
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return State{
			ConfigStatus:       "unknown",
			ServiceStatus:      "unknown",
			SubscriptionStatus: "not_configured",
		}, nil
	}
	if err != nil {
		return State{}, err
	}
	var result State
	if err := json.Unmarshal(data, &result); err != nil {
		return State{}, err
	}
	return result, nil
}

func Save(dir string, value State) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".monitor-state-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
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
	return os.Rename(tmpName, filepath.Join(dir, "monitor-state.json"))
}
