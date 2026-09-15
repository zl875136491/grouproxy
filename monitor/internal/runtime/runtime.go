package runtime

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
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

	mu         sync.Mutex
	process    *exec.Cmd
	done       chan struct{}
	activePath string
}

func (m *Manager) Check(configPath string) error {
	cmd := exec.Command(m.Binary, "check", "-c", configPath)
	if output, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("sing-box check: %w: %s", err, string(output))
	}
	return nil
}

func (m *Manager) processAliveLocked() bool {
	if m.process == nil || m.process.Process == nil {
		return false
	}
	if m.done == nil {
		return true
	}
	select {
	case <-m.done:
		return false
	default:
		return true
	}
}

func (m *Manager) stopLocked() {
	if m.process != nil && m.process.Process != nil {
		_ = m.process.Process.Signal(syscall.SIGTERM)
		if m.done != nil {
			select {
			case <-m.done:
			case <-time.After(3 * time.Second):
				_ = m.process.Process.Kill()
				<-m.done
			}
		} else {
			_, _ = m.process.Process.Wait()
		}
	}
	m.process = nil
	m.done = nil
}

func (m *Manager) stop() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.stopLocked()
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
	done := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(done)
	}()
	m.mu.Lock()
	m.process = cmd
	m.done = done
	m.activePath = configPath
	m.mu.Unlock()
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

func managedSingbox(cmdline []byte, binary, stateDir string) bool {
	if stateDir == "" {
		return false
	}
	parts := bytes.Split(bytes.TrimRight(cmdline, "\x00"), []byte{0})
	if len(parts) == 0 {
		return false
	}
	name := filepath.Base(string(parts[0]))
	if name != "sing-box" && string(parts[0]) != binary {
		return false
	}
	joined := string(bytes.ReplaceAll(cmdline, []byte{0}, []byte{' '}))
	return strings.Contains(joined, stateDir)
}

func (m *Manager) stopStrays() {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return
	}
	self := os.Getpid()
	m.mu.Lock()
	owned := 0
	if m.process != nil && m.process.Process != nil {
		owned = m.process.Process.Pid
	}
	binary := m.Binary
	stateDir := m.StateDir
	m.mu.Unlock()
	for _, entry := range entries {
		pid, convErr := strconv.Atoi(entry.Name())
		if convErr != nil || pid <= 1 || pid == self || pid == owned {
			continue
		}
		cmdline, readErr := os.ReadFile(filepath.Join("/proc", entry.Name(), "cmdline"))
		if readErr != nil || !managedSingbox(cmdline, binary, stateDir) {
			continue
		}
		proc, findErr := os.FindProcess(pid)
		if findErr != nil {
			continue
		}
		_ = proc.Signal(syscall.SIGTERM)
		deadline := time.Now().Add(3 * time.Second)
		for time.Now().Before(deadline) {
			if err := proc.Signal(syscall.Signal(0)); err != nil {
				break
			}
			time.Sleep(50 * time.Millisecond)
		}
		_ = proc.Kill()
	}
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
	m.stopStrays()
	if err := m.start(candidatePath); err != nil {
		if previous != "" {
			_ = m.start(previous)
		}
		return false, err
	}
	deadline := time.Now().Add(5 * time.Second)
	var portSeen time.Time
	for time.Now().Before(deadline) {
		m.mu.Lock()
		alive := m.processAliveLocked()
		m.mu.Unlock()
		if !alive {
			m.stop()
			if previous != "" {
				_ = m.start(previous)
				_ = waitPort(m.ListenPort, 5*time.Second)
			}
			return false, fmt.Errorf("sing-box exited before listening on %d", m.ListenPort)
		}
		if waitPort(m.ListenPort, 100*time.Millisecond) {
			if portSeen.IsZero() {
				portSeen = time.Now()
			}
			if time.Since(portSeen) >= 250*time.Millisecond {
				m.mu.Lock()
				alive = m.processAliveLocked()
				m.mu.Unlock()
				if alive {
					return true, nil
				}
				break
			}
			continue
		}
		portSeen = time.Time{}
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
	m.mu.Lock()
	processOK = m.processAliveLocked()
	m.mu.Unlock()
	portOK = waitPort(m.ListenPort, 500*time.Millisecond)
	apiOK = m.APIAddress == "" || waitAddress(m.APIAddress, 500*time.Millisecond)
	return
}
