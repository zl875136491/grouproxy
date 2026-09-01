"""Render the employee Linux setup script from the checked-in deployment template."""

from pathlib import Path

from ..config import Settings

_TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "deploy" / "linux-setup-proxy.sh"


def render_linux_setup_script(settings: Settings) -> str:
    """Use the same HTTP-only script for the download endpoint and repository."""

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    host_line = 'PROXY_HOST="${GROUPROXY_PROXY_HOST:-proxy.corp.internal}"'
    port_line = 'PROXY_PORT="${GROUPROXY_PROXY_PORT:-80}"'
    rendered_host_line = (
        f'PROXY_HOST="${{GROUPROXY_PROXY_HOST:-{settings.proxy_access_fqdn}}}"'
    )
    rendered_port_line = f'PROXY_PORT="${{GROUPROXY_PROXY_PORT:-{settings.proxy_access_port}}}"'
    if host_line not in template or port_line not in template:
        raise RuntimeError("linux_setup_template_markers_missing")
    return template.replace(host_line, rendered_host_line).replace(port_line, rendered_port_line)
