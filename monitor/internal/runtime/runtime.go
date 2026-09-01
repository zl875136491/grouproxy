package runtime

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type Manager struct {
	Binary     string
	ConfigPath string
	StateDir   string
	ListenPort int
	APIAddress string
	RunProcess bool
	process    *exec.Cmd
	activePath string
}

func (m *Manager) Check(configPath string) error {
	cmd := exec.Command(m.Binary, "check", "-c", configPath)
	if output, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("sing-box check: %w: %s", err, string(output))
	}
	return nil
}

func (m *Manager) stop() {
	if m.process == nil || m.process.Process == nil {
		return
	}
	_ = m.process.Process.Signal(syscall.SIGTERM)
	_, _ = m.process.Process.Wait()
	m.process = nil
}

func (m *Manager) start(configPath string) error {
	if !m.RunProcess {
		m.activePath = configPath
		return nil
	}
	logPath := filepath.Join(m.StateDir, "sing-box.log")
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	cmd := exec.Command(m.Binary, "run", "-c", configPath)
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		logFile.Close()
		return err
	}
	// The child inherits its descriptors; the monitor must close its copy so
	// rotated logs can be reclaimed and shutdown does not retain the handle.
	_ = logFile.Close()
	m.process = cmd
	m.activePath = configPath
	return nil
}

func waitAddress(address string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("tcp", address, 200*time.Millisecond)
		if err == nil {
			conn.Close()
			return true
		}
		time.Sleep(100 * time.Millisecond)
	}
	return false
}

func waitPort(port int, timeout time.Duration) bool {
	if _, known := linuxTCPListener(port); known {
		deadline := time.Now().Add(timeout)
		for time.Now().Before(deadline) {
			if listening, _ := linuxTCPListener(port); listening {
				return true
			}
			time.Sleep(100 * time.Millisecond)
		}
		return false
	}
	return waitAddress(fmt.Sprintf("127.0.0.1:%d", port), timeout)
}

// linuxTCPListener avoids creating an empty HTTP connection every time the
// monitor samples health. sing-box correctly logs such a connection as an EOF,
// but frequent health checks would otherwise flood the access log and obscure
// real authentication and routing events. Non-Linux builds retain the portable
// TCP fallback above.
func linuxTCPListener(port int) (listening, known bool) {
	for _, path := range []string{"/proc/net/tcp", "/proc/net/tcp6"} {
		file, err := os.Open(path)
		if err != nil {
			continue
		}
		known = true
		scanner := bufio.NewScanner(file)
		for scanner.Scan() {
			fields := strings.Fields(scanner.Text())
			if len(fields) < 4 || fields[3] != "0A" {
				continue
			}
			parts := strings.Split(fields[1], ":")
			if len(parts) != 2 {
				continue
			}
			parsed, parseErr := strconv.ParseInt(parts[1], 16, 32)
			if parseErr == nil && int(parsed) == port {
				_ = file.Close()
				return true, true
			}
		}
		_ = file.Close()
	}
	return false, known
}

func (m *Manager) Apply(candidatePath string) (bool, error) {
	if err := m.Check(candidatePath); err != nil {
		return false, err
	}
	if !m.RunProcess {
		m.activePath = candidatePath
		return true, nil
	}
	previous := m.activePath
	m.stop()
	if err := m.start(candidatePath); err != nil {
		if previous != "" {
			_ = m.start(previous)
		}
		return false, err
	}
	if waitPort(m.ListenPort, 5*time.Second) {
		return true, nil
	}
	m.stop()
	if previous != "" {
		_ = m.start(previous)
		_ = waitPort(m.ListenPort, 5*time.Second)
	}
	return false, fmt.Errorf("sing-box did not listen on %d", m.ListenPort)
}

func (m *Manager) Close() {
	m.stop()
}

func (m *Manager) Health(ctx context.Context) (processOK, portOK, apiOK bool) {
	if !m.RunProcess {
		return true, true, true
	}
	processOK = m.process != nil && m.process.ProcessState == nil
	portOK = waitPort(m.ListenPort, 500*time.Millisecond)
	apiOK = m.APIAddress == "" || waitAddress(m.APIAddress, 500*time.Millisecond)
	return
}
