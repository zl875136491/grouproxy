package subscription

import (
	"crypto/sha256"
	"fmt"
	"testing"
)

func TestParseSupportedFormatsAndHash(t *testing.T) {
	cases := []struct {
		name    string
		format  string
		content string
	}{
		{
			name:    "sing-box",
			format:  "sing-box",
			content: `{"outbounds":[{"type":"shadowsocks","tag":"edge-a","server":"198.51.100.20","server_port":8388,"method":"aes-256-gcm","password":"secret"}]}`,
		},
		{
			name:    "sip008",
			format:  "sip008",
			content: `{"version":1,"servers":[{"server":"198.51.100.20","server_port":8388,"method":"aes-256-gcm","password":"secret"}]}`,
		},
		{
			name:    "clash",
			format:  "clash",
			content: "proxies:\n  - name: edge-a\n    type: ss\n    server: 198.51.100.20\n    port: 8388\n    cipher: aes-256-gcm\n    password: secret\n",
		},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			content := []byte(test.content)
			digest := sha256.Sum256(content)
			if err := VerifyHash(content, fmt.Sprintf("%x", digest[:])); err != nil {
				t.Fatalf("VerifyHash() error = %v", err)
			}
			outbounds, err := Parse(content, test.format)
			if err != nil {
				t.Fatalf("Parse() error = %v", err)
			}
			if len(outbounds) != 1 {
				t.Fatalf("outbound count = %d", len(outbounds))
			}
		})
	}
}

func TestParseRejectsControlOwnedOutbound(t *testing.T) {
	_, err := Parse([]byte(`[{"type":"direct","tag":"direct"}]`), "sing-box")
	if err == nil || err.Error() != "subscription_outbound_type_unsupported" {
		t.Fatalf("Parse() error = %v", err)
	}
}

func TestParseClashTrojanNormalizesTLSFields(t *testing.T) {
	content := []byte(`proxies:
  - name: edge-a
    type: trojan
    server: edge.example.net
    port: 443
    password: secret
    sni: cdn.example.net
    skip-cert-verify: true
    client-fingerprint: chrome
    alpn: [h2, http/1.1]
`)

	outbounds, err := Parse(content, "clash")
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	if len(outbounds) != 1 {
		t.Fatalf("outbound count = %d", len(outbounds))
	}
	outbound := outbounds[0].(map[string]any)
	tls, ok := outbound["tls"].(map[string]any)
	if !ok {
		t.Fatalf("TLS object was not rendered: %#v", outbound["tls"])
	}
	if tls["enabled"] != true || tls["server_name"] != "cdn.example.net" || tls["insecure"] != true {
		t.Fatalf("TLS values = %#v", tls)
	}
	alpn, ok := tls["alpn"].([]string)
	if !ok || len(alpn) != 2 || alpn[0] != "h2" || alpn[1] != "http/1.1" {
		t.Fatalf("TLS ALPN = %#v", tls["alpn"])
	}
	utls, ok := tls["utls"].(map[string]any)
	if !ok || utls["enabled"] != true || utls["fingerprint"] != "chrome" {
		t.Fatalf("TLS uTLS = %#v", tls["utls"])
	}
}
