"""Select immutable employee access assets for the active deployment."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config import Settings

_DEPLOY_PATH = Path(__file__).resolve().parents[3] / "deploy"


@dataclass(frozen=True)
class AccessProfile:
    environment: Literal["test", "production"]
    fqdn: str
    macos_shortcut_url: str
    linux_script_path: Path
    windows_script_path: Path


_ACCESS_PROFILES: dict[str, AccessProfile] = {
    "test": AccessProfile(
        environment="test",
        fqdn="test-proxy.1oa.com.cn",
        macos_shortcut_url="/shortcuts/grouproxy-macos-test.shortcut",
        linux_script_path=_DEPLOY_PATH / "linux-setup-proxy-test.sh",
        windows_script_path=_DEPLOY_PATH / "windows-setup-proxy-test.ps1",
    ),
    "production": AccessProfile(
        environment="production",
        fqdn="proxy.1oa.com.cn",
        macos_shortcut_url="/shortcuts/grouproxy-macos-production.shortcut",
        linux_script_path=_DEPLOY_PATH / "linux-setup-proxy.sh",
        windows_script_path=_DEPLOY_PATH / "windows-setup-proxy.ps1",
    ),
}


def access_profile(settings: Settings) -> AccessProfile:
    """Map every non-test deployment to the production access assets."""

    # ``getattr`` keeps this selector compatible with lightweight settings
    # doubles used by maintenance scripts while the real Pydantic settings
    # object always provides ``environment``.
    environment = str(getattr(settings, "environment", "production"))
    profile_name = "test" if environment.strip().lower() == "test" else "production"
    return _ACCESS_PROFILES[profile_name]


def load_linux_setup_script(settings: Settings) -> str:
    return access_profile(settings).linux_script_path.read_text(encoding="utf-8")


def load_windows_setup_script(settings: Settings) -> str:
    return access_profile(settings).windows_script_path.read_text(encoding="utf-8")
