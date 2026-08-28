package runtime

import (
	"os"
	"path/filepath"
	"testing"
)

func TestStartClosesMonitorCopyOfLogFile(t *testing.T) {
	dir := t.TempDir()
	binary := filepath.Join(dir, "fake-sing-box")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\nsleep 30\n"), 0o700); err != nil {
		t.Fatal(err)
	}

	manager := &Manager{Binary: binary, StateDir: dir, RunProcess: true}
	if err := manager.start("unused.json"); err != nil {
		t.Fatalf("start: %v", err)
	}
	defer manager.Close()

	logPath := filepath.Join(dir, "sing-box.log")
	if err := os.Rename(logPath, logPath+".rotated"); err != nil {
		t.Fatalf("log descriptor was not releasable: %v", err)
	}
}
