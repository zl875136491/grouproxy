package runtime

import (
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
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

func TestLinuxTCPListenerFindsBoundPortWithoutDialingIt(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	port := listener.Addr().(*net.TCPAddr).Port

	listening, known := linuxTCPListener(port)
	if !known {
		t.Skip("/proc TCP listener state is not available on this platform")
	}
	if !listening {
		t.Fatalf("listener on %d was not discovered", port)
	}
	if !waitPort(port, 50*time.Millisecond) {
		t.Fatalf("waitPort did not discover listener on %d", port)
	}
}
