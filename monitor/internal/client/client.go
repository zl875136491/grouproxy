package client

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

type Client struct {
	BaseURL    string
	Token      string
	HTTPClient *http.Client
}

type DesiredResponse struct {
	DesiredStale bool           `json:"desired_stale"`
	ReleaseID    string         `json:"release_id"`
	Bundle       map[string]any `json:"bundle"`
}

func New(baseURL, tokenFile string) (*Client, error) {
	data, err := os.ReadFile(tokenFile)
	if err != nil {
		return nil, err
	}
	token := strings.TrimSpace(string(data))
	if token == "" {
		return nil, fmt.Errorf("empty token file")
	}
	return &Client{BaseURL: strings.TrimRight(baseURL, "/"), Token: token, HTTPClient: &http.Client{Timeout: 10 * time.Second}}, nil
}

func (c *Client) request(method, path string, query url.Values, body any, out any) error {
	endpoint := c.BaseURL + path
	if len(query) > 0 {
		endpoint += "?" + query.Encode()
	}
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, endpoint, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("agent http %d: %s", resp.StatusCode, strings.TrimSpace(string(data)))
	}
	if out != nil && len(data) > 0 {
		if err := json.Unmarshal(data, out); err != nil {
			return err
		}
	}
	return nil
}

func (c *Client) Desired(nodeID string, appliedVersion int, appliedHash string) (DesiredResponse, error) {
	query := url.Values{}
	query.Set("applied_version", fmt.Sprint(appliedVersion))
	query.Set("applied_hash", appliedHash)
	var result DesiredResponse
	err := c.request(http.MethodGet, "/agent/v1/desired", query, nil, &result)
	return result, err
}

func (c *Client) Heartbeat(payload any) error {
	return c.request(http.MethodPost, "/agent/v1/heartbeat", nil, payload, nil)
}

func (c *Client) Ack(payload any) error {
	return c.request(http.MethodPost, "/agent/v1/ack", nil, payload, nil)
}
