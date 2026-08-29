package routingdata

import (
	"os"
	"path/filepath"
	"testing"
)

func TestEnsureWritesAndRepairsPinnedAssets(t *testing.T) {
	stateDir := t.TempDir()
	if err := Ensure(stateDir); err != nil {
		t.Fatalf("Ensure() error = %v", err)
	}
	for _, item := range assets {
		path := Path(stateDir, item.tag)
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("read %s: %v", item.filename, err)
		}
		if string(hash(data)) != string(hash(item.content)) {
			t.Fatalf("%s did not match embedded data", item.filename)
		}
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat %s: %v", item.filename, err)
		}
		if info.Mode().Perm() != 0o600 {
			t.Fatalf("%s mode = %o, want 600", item.filename, info.Mode().Perm())
		}
	}

	corrupted := filepath.Join(Dir(stateDir), "geoip-cn.srs")
	if err := os.WriteFile(corrupted, []byte("invalid"), 0o600); err != nil {
		t.Fatalf("corrupt asset: %v", err)
	}
	if err := Ensure(stateDir); err != nil {
		t.Fatalf("Ensure() repair error = %v", err)
	}
	data, err := os.ReadFile(corrupted)
	if err != nil {
		t.Fatalf("read repaired asset: %v", err)
	}
	if string(hash(data)) != string(hash(geoIPCN)) {
		t.Fatal("Ensure() did not repair the corrupted geoip asset")
	}
}
