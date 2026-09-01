const MAX_HEADER_BYTES = 32 * 1024;

// sing-box 1.13 removed inbound PROXY protocol. The test stream guard
// therefore applies the proxy source CIDRs before forwarding to its loopback
// listener; the generated sing-box ACL remains a defense against direct
// access because the listener is never externally reachable.
const PROXY_ALLOW_CIDRS = "@@PROXY_ALLOW_CIDRS@@".split(",");

function normalizeAuthority(value) {
  let authority = String(value || "").trim().toLowerCase();
  if (!authority || /[\s\/?#@]/.test(authority)) return "";

  // Handle bracketed IPv6 authorities without mistaking an address suffix
  // for a TCP port. Host names use the simpler host[:port] form.
  if (authority.startsWith("[")) {
    const closing = authority.indexOf("]");
    if (closing < 0) return "";
    const suffix = authority.slice(closing + 1);
    if (suffix && !/^:\d{1,5}$/.test(suffix)) return "";
    return authority.slice(1, closing);
  }

  if (authority.indexOf(":") >= 0 && authority.indexOf(":") === authority.lastIndexOf(":")) {
    const separator = authority.lastIndexOf(":");
    const port = authority.slice(separator + 1);
    if (!/^\d{1,5}$/.test(port) || Number(port) > 65535) return "";
    authority = authority.slice(0, separator);
  }
  return authority;
}

function ipv4Number(value) {
  const parts = value.split(".");
  if (parts.length !== 4) return -1;
  let number = 0;
  for (let index = 0; index < parts.length; index += 1) {
    const part = Number(parts[index]);
    if (!isFinite(part) || Math.floor(part) !== part || part < 0 || part > 255) return -1;
    number = number * 256 + part;
  }
  return number;
}

function sourceAllowed(address) {
  let source = String(address || "").trim();
  if (source.startsWith("[") && source.indexOf("]") > 0) {
    source = source.slice(1, source.indexOf("]"));
  } else if (source.indexOf(":") === source.lastIndexOf(":")) {
    const separator = source.lastIndexOf(":");
    if (/^\d+$/.test(source.slice(separator + 1))) source = source.slice(0, separator);
  }
  const sourceNumber = ipv4Number(source);
  for (let index = 0; index < PROXY_ALLOW_CIDRS.length; index += 1) {
    const entry = PROXY_ALLOW_CIDRS[index].trim();
    if (!entry) continue;
    const parts = entry.split("/");
    const networkNumber = ipv4Number(parts[0]);
    const prefix = parts.length === 1 ? 32 : Number(parts[1]);
    if (sourceNumber < 0 || networkNumber < 0 || !isFinite(prefix) || Math.floor(prefix) !== prefix || prefix < 0 || prefix > 32) continue;
    const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
    if (((sourceNumber >>> 0) & mask) === ((networkNumber >>> 0) & mask)) return true;
  }
  return false;
}

function dashboardTarget(target, expectedHost) {
  if (target.startsWith("/")) return target;

  // Browsers configured with an HTTP proxy send absolute-form targets. Only
  // the test dashboard authority is eligible for the control-plane route;
  // every other absolute URI remains a forward-proxy request.
  const absolute = target.match(
    /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([^\/?#]+)(\/[^?#]*)?(?:\?([^#]*))?$/,
  );
  if (!absolute || absolute[1].toLowerCase() !== "http") return "";
  if (normalizeAuthority(absolute[2]) !== expectedHost) return "";
  return (absolute[3] || "/") + (absolute[4] === undefined ? "" : `?${absolute[4]}`);
}

function dashboardRequest(header, expectedHost) {
  const lines = header.split("\r\n");
  const requestLine = lines.shift() || "";
  const request = requestLine.match(/^([A-Z]+)\s+(\S+)\s+HTTP\/1\.[01]$/);
  if (!request) return false;

  let host = "";
  let hostSeen = false;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const separator = line.indexOf(":");
    if (separator < 1) continue;
    if (line.slice(0, separator).trim().toLowerCase() === "host") {
      if (hostSeen) return false;
      hostSeen = true;
      host = normalizeAuthority(line.slice(separator + 1));
    }
  }
  if (!expectedHost || !hostSeen || host !== expectedHost) return false;

  const method = request[1];
  if (method === "CONNECT") return false;
  const path = dashboardTarget(request[2], expectedHost);
  if (!path) return false;

  const pathname = path.split("?", 1)[0];
  return pathname === "/dashboard" || pathname.startsWith("/dashboard/");
}

function preread(session) {
  let buffered = "";
  let completed = false;

  function finish(route) {
    if (completed) return;
    completed = true;
    session.variables.grouproxy_route = route;
    // Resolve the route and the proxy-only source policy in the same preread
    // callback. js_access can run before an asynchronous upload callback has
    // updated the route variable, which would make dashboard access depend on
    // the loopback address being present in the proxy CIDR list.
    if (route === "dashboard" || sourceAllowed(session.remoteAddress)) {
      session.allow();
    } else {
      session.deny();
    }
  }

  session.on("upload", (data, flags) => {
    buffered += data.toString();
    const headerEnd = buffered.indexOf("\r\n\r\n");
    if (headerEnd < 0 && buffered.length < MAX_HEADER_BYTES && !flags.last) return;
    if (headerEnd < 0) {
      finish("proxy");
      return;
    }
    const expectedHost = normalizeAuthority(session.variables.grouproxy_dashboard_host || "");
    finish(dashboardRequest(buffered.slice(0, headerEnd), expectedHost) ? "dashboard" : "proxy");
  });
}

export default { preread };
