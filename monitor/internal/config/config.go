package config

import (
	"errors"
	"net"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

type Config struct {
	BackendURL                 string `yaml:"backend_url"`
	NodeID                     string `yaml:"node_id"`
	TokenFile                  string `yaml:"token_file"`
	StateDir                   string `yaml:"state_dir"`
	SingboxBin                 string `yaml:"singbox_bin"`
	SingboxConfig              string `yaml:"singbox_config"`
	ListenPort                 int    `yaml:"listen_port"`
	ListenPortOverride         int    `yaml:"listen_port_override"`
	ListenAddressOverride      string `yaml:"listen_address_override"`
	FirewallPortOverride       int    `yaml:"firewall_port_override"`
	FirewallMode               string `yaml:"firewall_mode"` // dry-run or apply
	PollIntervalSeconds        int    `yaml:"poll_interval_seconds"`
	HeartbeatIntervalSeconds   int    `yaml:"heartbeat_interval_seconds"`
	RunSingbox                 bool   `yaml:"run_singbox"`
	HMACSecret                 string `yaml:"hmac_secret"`
	AllowInsecureHTTP          bool   `yaml:"allow_insecure_http"`
	ClashAPIListen             string `yaml:"clash_api_listen"`
	HealthWindowSeconds        int    `yaml:"health_window_seconds"`
	HealthSampleSeconds        int    `yaml:"health_sample_seconds"`
	SpoolMaxBytes              int64  `yaml:"spool_max_bytes"`
	TelemetryBatchMax          int    `yaml:"telemetry_batch_max"`
	ProbeMaxOutbounds          int    `yaml:"probe_max_outbounds"`
	ProxyConfigIntervalSeconds int    `yaml:"proxy_config_interval_seconds"`
	ProxyDelayIntervalSeconds  int    `yaml:"proxy_delay_interval_seconds"`
}

func (c *Config) Defaults() {
	if c.StateDir == "" {
		c.StateDir = "/var/lib/grouproxy"
	}
	if c.SingboxConfig == "" {
		c.SingboxConfig = filepath.Join(c.StateDir, "sing-box.json")
	}
	if c.ListenPort == 0 {
		c.ListenPort = 80
	}
	if c.ListenPortOverride < 0 || c.ListenPortOverride > 65535 {
		c.ListenPortOverride = 0
	}
	if c.FirewallPortOverride < 0 || c.FirewallPortOverride > 65535 {
		c.FirewallPortOverride = 0
	}
	if c.FirewallMode == "" {
		c.FirewallMode = "dry-run"
	}
	if c.PollIntervalSeconds <= 0 {
		c.PollIntervalSeconds = 15
	}
	if c.HeartbeatIntervalSeconds <= 0 {
		c.HeartbeatIntervalSeconds = 15
	}
	if c.HMACSecret == "" {
		c.HMACSecret = os.Getenv("GROUPROXY_BUNDLE_HMAC_SECRET")
	}
	if c.ClashAPIListen == "" {
		c.ClashAPIListen = "127.0.0.1:9090"
	}
	if c.HealthWindowSeconds <= 0 {
		c.HealthWindowSeconds = 30
	}
	if c.HealthSampleSeconds <= 0 {
		c.HealthSampleSeconds = 2
	}
	if c.SpoolMaxBytes <= 0 {
		c.SpoolMaxBytes = 50 << 20
	}
	if c.TelemetryBatchMax <= 0 || c.TelemetryBatchMax > 500 {
		c.TelemetryBatchMax = 200
	}
	if c.ProbeMaxOutbounds <= 0 || c.ProbeMaxOutbounds > 20 {
		c.ProbeMaxOutbounds = 3
	}
	if c.ProxyConfigIntervalSeconds <= 0 {
		c.ProxyConfigIntervalSeconds = 15
	}
	if c.ProxyDelayIntervalSeconds <= 0 {
		c.ProxyDelayIntervalSeconds = 60
	}
}

func Load(path string) (Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, err
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return Config{}, err
	}
	cfg.Defaults()
	if cfg.BackendURL == "" || cfg.NodeID == "" || cfg.TokenFile == "" || cfg.SingboxBin == "" {
		return Config{}, errors.New("backend_url, node_id, token_file and singbox_bin are required")
	}
	if len(cfg.HMACSecret) < 32 {
		return Config{}, errors.New("hmac_secret or GROUPROXY_BUNDLE_HMAC_SECRET must be at least 32 bytes")
	}
	if cfg.FirewallMode != "dry-run" && cfg.FirewallMode != "apply" {
		return Config{}, errors.New("firewall_mode must be dry-run or apply")
	}
	if cfg.ListenPort < 1 || cfg.ListenPort > 65535 {
		return Config{}, errors.New("listen_port must be between 1 and 65535")
	}
	if cfg.ListenAddressOverride != "" {
		address := net.ParseIP(cfg.ListenAddressOverride)
		if address == nil {
			return Config{}, errors.New("listen_address_override must be an IP address")
		}
	}
	host, port, err := net.SplitHostPort(cfg.ClashAPIListen)
	if err != nil || host == "" || port == "" {
		return Config{}, errors.New("clash_api_listen must be host:port")
	}
	address := net.ParseIP(host)
	if address == nil || !address.IsLoopback() {
		return Config{}, errors.New("clash_api_listen must use a loopback address")
	}
	apiPort, err := net.LookupPort("tcp", port)
	if err != nil || apiPort < 1 || apiPort > 65535 {
		return Config{}, errors.New("clash_api_listen port must be between 1 and 65535")
	}
	if !cfg.AllowInsecureHTTP && len(cfg.BackendURL) >= 7 && cfg.BackendURL[:7] == "http://" {
		return Config{}, errors.New("http backend requires allow_insecure_http=true")
	}
	return cfg, nil
}
