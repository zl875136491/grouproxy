"use client";

import { useEffect, useMemo, useState } from "react";
import { ArrowUpRight, Check, CircleAlert, GitBranch, LockKeyhole, Network, RefreshCw, ShieldCheck, Sparkles } from "lucide-react";
import { Badge, Button, Card } from "../components/ui";
import { getNodes, getOverview, getSites, managementToken, previewCIDR, type Node, type Site } from "../lib/api";

type Overview = { sites: number; nodes: number; online_nodes: number; in_sync_nodes: number; drifted_nodes: number; connections: number; http_only: boolean };

const demoSites: Site[] = [
  { id: "north", slug: "north", name: "North Region", shutdown: false, config_revision: 42, http_port: 80 },
  { id: "east", slug: "east", name: "East Region", shutdown: false, config_revision: 42, http_port: 80 },
  { id: "south", slug: "south", name: "South Region", shutdown: false, config_revision: 41, http_port: 80 },
  { id: "west", slug: "west", name: "West Region", shutdown: false, config_revision: 42, http_port: 80 },
  { id: "central", slug: "central", name: "Central Region", shutdown: false, config_revision: 42, http_port: 80 },
];

const demoNodes: Node[] = [
  { id: "n1", site_id: "north", name: "codedev", agent_id: "codedev", monitor_version: "0.1.0", applied_version: 42, desired_version: 42, liveness_status: "online", config_status: "in_sync", service_status: "healthy", last_error: "" },
  { id: "n2", site_id: "east", name: "nuc", agent_id: "nuc", monitor_version: "0.1.0", applied_version: 42, desired_version: 42, liveness_status: "online", config_status: "in_sync", service_status: "healthy", last_error: "" },
];

const demoOverview: Overview = { sites: 5, nodes: 2, online_nodes: 2, in_sync_nodes: 2, drifted_nodes: 0, connections: 18, http_only: true };

function formatNumber(value: number) { return new Intl.NumberFormat("en-US").format(value); }

export default function Dashboard() {
  const [overview, setOverview] = useState<Overview>(demoOverview);
  const [sites, setSites] = useState<Site[]>(demoSites);
  const [nodes, setNodes] = useState<Node[]>(demoNodes);
  const [apiState, setApiState] = useState<"live" | "demo" | "error">("demo");
  const [activeTab, setActiveTab] = useState("Access policy");
  const [sourceIP, setSourceIP] = useState("10.32.12.111");
  const [selectedSite, setSelectedSite] = useState("north");
  const [preview, setPreview] = useState<{ allowed: boolean; reason: string; matched_cidr: string | null } | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);

  useEffect(() => {
    if (!managementToken()) return;
    Promise.all([getOverview(), getSites(), getNodes()]).then(([nextOverview, nextSites, nextNodes]) => {
      setOverview(nextOverview); setSites(nextSites); setNodes(nextNodes); setApiState("live");
    }).catch(() => setApiState("error"));
  }, []);

  const siteByID = useMemo(() => new Map(sites.map((site) => [site.id, site])), [sites]);
  const synced = nodes.filter((node) => node.config_status === "in_sync").length;

  async function runPreview() {
    setPreviewBusy(true);
    try {
      const result = await previewCIDR(selectedSite, sourceIP);
      setPreview({ allowed: result.allowed, reason: result.reason, matched_cidr: result.matched_cidr });
    } catch {
      const allowed = sourceIP.startsWith("10.32.");
      setPreview({ allowed, reason: allowed ? "demo_allow" : "not_in_allowlist", matched_cidr: allowed ? "10.32.0.0/16" : null });
    } finally { setPreviewBusy(false); }
  }

  return (
    <div className="shell">
      <header className="hero">
        <nav className="nav">
          <a className="brand" href="#top"><span className="brand-mark" aria-hidden="true" /><span>grouproxy</span></a>
          <div className="nav-links"><a href="#overview">Overview</a><a href="#policy">Access policy</a><a href="#releases">Releases</a><a href="#nodes">Nodes</a></div>
          <div className="nav-actions"><a className="button button-dark" href="/login">Sign in</a><a className="button button-primary" href="#policy">Open console <ArrowUpRight size={15} /></a></div>
        </nav>
        <div className="hero-inner" id="top">
          <div>
            <p className="eyebrow">Regional proxy control plane</p>
            <h1>One control plane. Every region in sync.</h1>
            <p className="hero-copy">Coordinate source network access, signed releases, and node health from one calm operating surface.</p>
            <div className="hero-cta"><a className="button button-primary" href="#overview">View operations <ArrowUpRight size={15} /></a><a className="button button-dark" href="#releases">Inspect latest release</a></div>
          </div>
          <div className="hero-panel" aria-label="Current release summary">
            <div className="panel-top"><span>Current desired bundle</span><span className="live-dot">{apiState === "live" ? "Live" : "Local preview"}</span></div>
            <pre className="code"><span className="key">{"{"}</span>{"\n  "}<span className="key">"desired_version"</span>: <span className="value">{Math.max(...sites.map((site) => site.config_revision), 42)}</span>,{"\n  "}<span className="key">"listener"</span>: <span className="value">"http://:80"</span>,{"\n  "}<span className="key">"nodes_in_sync"</span>: <span className="value">{synced}</span>,{"\n  "}<span className="key">"transport"</span>: <span className="value">"bearer + hmac"</span>{"\n"}<span className="key">{"}"}</span></pre>
          </div>
        </div>
      </header>

      <main className="main">
        <section className="status-rail" aria-label="Regional status">
          {sites.map((site) => <div className="site-status" key={site.id}><div className="site-status-top"><span className="site-name">{site.name}</span><Badge tone={site.shutdown ? "warn" : "default"}>{site.shutdown ? "Paused" : "Healthy"}</Badge></div><div className="status-meta">Bundle v{site.config_revision} - HTTP :{site.http_port}</div></div>)}
        </section>

        <section className="section" id="overview">
          <div className="section-heading"><div><p className="eyebrow" style={{ color: "var(--green-dark)", marginBottom: 10 }}>Operations at a glance</p><h2>Keep the edge predictable.</h2></div><p>Facts arrive from each monitor independently, so release state and service health stay distinct.</p></div>
          <div className="metric-grid">
            <div className="metric"><div className="metric-label">Regions configured</div><div className="metric-value">{overview.sites}</div><div className="metric-note">Five-site baseline</div></div>
            <div className="metric"><div className="metric-label">Nodes online</div><div className="metric-value">{overview.online_nodes}<span style={{ color: "var(--muted)", fontSize: 20 }}> / {overview.nodes}</span></div><div className="metric-note">Heartbeat within window</div></div>
            <div className="metric"><div className="metric-label">In sync</div><div className="metric-value">{overview.in_sync_nodes}</div><div className="metric-note">Signed config applied</div></div>
            <div className="metric"><div className="metric-label">Active connections</div><div className="metric-value">{formatNumber(overview.connections)}</div><div className="metric-note neutral">HTTP :80 data path</div></div>
          </div>
        </section>

        <section className="section module-grid" id="policy">
          <Card>
            <div className="tabs">{["Access policy", "Release flow", "Node health"].map((tab) => <button className={`tab${activeTab === tab ? " active" : ""}`} key={tab} onClick={() => setActiveTab(tab)}>{tab}</button>)}</div>
            {activeTab === "Access policy" && <><h3>Source access, resolved once</h3><p className="module-subtitle">The same effective CIDR list drives nftables and sing-box routing.</p><div className="feature-list"><div className="feature"><span className="feature-icon"><Network size={16} /></span><div><strong>Site-local allow lists</strong><span>Regional networks stay scoped to their own node.</span></div></div><div className="feature"><span className="feature-icon"><LockKeyhole size={16} /></span><div><strong>Travel exceptions expire</strong><span>Temporary /32 access is visible and time bounded.</span></div></div><div className="feature"><span className="feature-icon"><ShieldCheck size={16} /></span><div><strong>Fail closed by default</strong><span>Empty or invalid policy never widens the listener.</span></div></div></div></>}
            {activeTab === "Release flow" && <><h3>Draft to applied</h3><p className="module-subtitle">Every configuration change has a version, ACK, and rollback boundary.</p><div className="feature-list"><div className="feature"><span className="feature-icon"><GitBranch size={16} /></span><div><strong>Validate before publish</strong><span>Canonical diff, risk, sing-box check, and nft dry-run.</span></div></div><div className="feature"><span className="feature-icon"><Check size={16} /></span><div><strong>Independent node ACKs</strong><span>One node can fail without hiding another node's result.</span></div></div></div></>}
            {activeTab === "Node health" && <><h3>Service facts, not a single green dot</h3><p className="module-subtitle">Liveness, configuration, and service status are tracked separately.</p><div className="feature-list"><div className="feature"><span className="feature-icon"><RefreshCw size={16} /></span><div><strong>Heartbeat and drift</strong><span>Monitor reports applied version and the last useful error.</span></div></div><div className="feature"><span className="feature-icon"><CircleAlert size={16} /></span><div><strong>Health window rollback</strong><span>Failed listeners return to the last-good bundle.</span></div></div></div></>}
          </Card>
          <Card className="dark"><p className="eyebrow">Fast path</p><h3>HTTP CONNECT on :80</h3><p className="module-subtitle">The first release keeps the employee path simple and explicit. TLS to destination sites remains end-to-end inside the tunnel.</p><div className="code"><span className="key">proxy_host</span>: proxy.corp.internal<br /><span className="key">proxy_port</span>: <span className="value">80</span><br /><span className="key">control_plane</span>: codedev only<br /><span className="key">agents</span>: codedev, nuc</div><div style={{ marginTop: 22 }}><span className="status-pill">HTTPS listener disabled</span></div></Card>
        </section>

        <section className="section" id="releases"><div className="section-heading"><div><p className="eyebrow" style={{ color: "var(--green-dark)", marginBottom: 10 }}>Release ledger</p><h2>See what changed and where.</h2></div><a className="button" style={{ border: "1px solid var(--line)", background: "#fff" }} href="#nodes">Open release history <ArrowUpRight size={15} /></a></div><div className="module-grid"><Card><h3>Latest release</h3><p className="module-subtitle">Desired v{Math.max(...sites.map((site) => site.config_revision), 42)} - policy revision {Math.max(...sites.map((site) => site.config_revision), 42)}</p><div className="timeline"><div className="timeline-row"><span className="timeline-marker" /><div><div className="timeline-title">Bundle generated</div><div className="timeline-detail">Canonical JSON and HMAC verified</div></div><span className="timeline-time">09:42 UTC</span></div><div className="timeline-row"><span className="timeline-marker" /><div><div className="timeline-title">nft dry-run passed</div><div className="timeline-detail">Proxy port restricted to effective CIDRs</div></div><span className="timeline-time">09:42 UTC</span></div><div className="timeline-row"><span className="timeline-marker" /><div><div className="timeline-title">ACK received from codedev</div><div className="timeline-detail">Applied hash matches desired hash</div></div><span className="timeline-time">09:43 UTC</span></div><div className="timeline-row"><span className="timeline-marker pending" /><div><div className="timeline-title">nuc heartbeat pending</div><div className="timeline-detail">Last check is still inside the grace window</div></div><span className="timeline-time">now</span></div></div></Card><Card className="dark"><p className="eyebrow">Bundle contract</p><h3>Signed state, local ownership.</h3><p className="module-subtitle">The control plane computes desired state. Monitors own local firewall and sing-box changes.</p><div className="code"><span className="key">hash</span>: sha256(canonical_json)<br /><span className="key">mac</span>: hmac-sha256(bundle)<br /><span className="key">rollback</span>: last-good<br /><span className="key">expiry</span>: enforced</div></Card></div></section>

        <section className="section" id="nodes"><div className="section-heading"><div><p className="eyebrow" style={{ color: "var(--green-dark)", marginBottom: 10 }}>Node inventory</p><h2>Each monitor tells its own story.</h2></div><p>{apiState === "error" ? "Backend is unavailable; showing the safe local preview." : apiState === "live" ? "Live control-plane data" : "Preview data until a management token is configured"}</p></div><div className="table-wrap"><table><thead><tr><th>Node</th><th>Liveness</th><th>Config</th><th>Applied / desired</th><th>Monitor</th></tr></thead><tbody>{nodes.map((node) => <tr key={node.id}><td><div className="node-name">{node.name}</div><div className="node-site">{siteByID.get(node.site_id)?.name || node.site_id}</div></td><td><span className={`state${node.liveness_status !== "online" ? " degraded" : ""}`}>{node.liveness_status}</span></td><td><span className={`state${node.config_status !== "in_sync" ? " degraded" : ""}`}>{node.config_status}</span></td><td>{node.applied_version} / {node.desired_version}</td><td>{node.monitor_version}</td></tr>)}</tbody></table></div></section>

        <section className="section module-grid"><Card><h3>Source IP preview</h3><p className="module-subtitle">Run the same policy calculation used for a new bundle.</p><div className="preview"><div className="field"><label htmlFor="source-ip">Source IP</label><input id="source-ip" value={sourceIP} onChange={(event) => setSourceIP(event.target.value)} placeholder="10.32.12.111" /></div><div className="field"><label htmlFor="site">Target site</label><select id="site" value={selectedSite} onChange={(event) => setSelectedSite(event.target.value)} style={{ height: 42, border: "1px solid var(--line)", borderRadius: 7, padding: "0 10px", background: "#fff" }}>{sites.map((site) => <option value={site.id} key={site.id}>{site.name}</option>)}</select></div><Button className="button-primary" onClick={runPreview} disabled={previewBusy}>{previewBusy ? "Checking..." : "Check access"}</Button></div>{preview && <div className={`preview-result ${preview.allowed ? "allowed" : "denied"}`}>{preview.allowed ? `Allowed by ${preview.matched_cidr || "effective policy"}` : `Denied: ${preview.reason}`}</div>}</Card><Card className="dark"><p className="eyebrow">Operator note</p><h3>Keep the first path boring.</h3><p className="module-subtitle">This environment intentionally exposes HTTP proxy :80 only. HTTPS proxy, identity providers, and CI/CD remain outside Phase 0 and Phase 1.</p><div className="feature"><span className="feature-icon"><Sparkles size={16} /></span><div><strong>Ready for the next phase</strong><span>Subscriptions and richer observability can build on the same release contract.</span></div></div></Card></section>

        <footer className="footer"><strong>grouproxy control plane</strong><span>Phase 0 - Phase 1 - HTTP transport test environment - No CI/CD changes</span></footer>
      </main>
    </div>
  );
}
