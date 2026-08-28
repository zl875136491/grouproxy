package state

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSaveLoadUsesPrivateAtomicFile(t *testing.T) {
	dir := t.TempDir()
	want := State{LastGoodVersion: 4, LastGoodHash: "abc", ConfigStatus: "in_sync", ServiceStatus: "healthy"}
	if err := Save(dir, want); err != nil {
		t.Fatalf("Save() error = %v", err)
	}
	got, err := Load(dir)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if got.LastGoodVersion != want.LastGoodVersion || got.LastGoodHash != want.LastGoodHash {
		t.Fatalf("loaded state = %+v", got)
	}
	info, err := os.Stat(filepath.Join(dir, "monitor-state.json"))
	if err != nil {
		t.Fatalf("stat state: %v", err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("state mode = %o, want 600", info.Mode().Perm())
	}
}
