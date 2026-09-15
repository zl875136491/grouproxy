#!/usr/bin/env bash
set -Eeuo pipefail

# Run this file without arguments to toggle the current user's proxy state.
PROXY_HOST="${GROUPROXY_PROXY_HOST:-proxy.1oa.com.cn}"
PROXY_PORT="${GROUPROXY_PROXY_PORT:-1080}"
NO_PROXY_VALUE="${GROUPROXY_NO_PROXY:-localhost,127.0.0.1,::1,.corp.internal,${PROXY_HOST},10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"

if (( $# != 0 )); then
  printf 'This proxy toggle does not accept parameters. Run the file without arguments.\n' >&2
  exit 2
fi
if [[ ! "$PROXY_HOST" =~ ^[A-Za-z0-9.-]+$ ]]; then
  printf 'GROUPROXY_PROXY_HOST is invalid.\n' >&2
  exit 2
fi
if [[ ! "$PROXY_PORT" =~ ^[0-9]+$ ]] || (( PROXY_PORT < 1 || PROXY_PORT > 65535 )); then
  printf 'GROUPROXY_PROXY_PORT must be between 1 and 65535.\n' >&2
  exit 2
fi
if [[ ! "$NO_PROXY_VALUE" =~ ^[A-Za-z0-9.,:/_-]+$ ]]; then
  printf 'GROUPROXY_NO_PROXY contains unsupported characters.\n' >&2
  exit 2
fi
if [[ -z "${HOME:-}" ]]; then
  printf 'HOME is required to update the current user proxy settings.\n' >&2
  exit 2
fi

proxy_url="http://${PROXY_HOST}:${PROXY_PORT}"
config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
managed_dir="$config_root/grouproxy"
shell_file="$managed_dir/proxy.env"
environment_file="$config_root/environment.d/90-grouproxy-proxy.conf"

is_managed_file() {
  local path="$1"
  [[ -f "$path" ]] && grep -Fqx '# Managed by Grouproxy proxy toggle.' "$path"
}

gnome_proxy_is_enabled() {
  command -v gsettings >/dev/null 2>&1 || return 1
  local mode
  mode="$(gsettings get org.gnome.system.proxy mode 2>/dev/null)" || return 1
  [[ "$mode" != "'none'" ]]
}

kde_writer() {
  if command -v kwriteconfig6 >/dev/null 2>&1; then
    printf '%s\n' kwriteconfig6
  elif command -v kwriteconfig5 >/dev/null 2>&1; then
    printf '%s\n' kwriteconfig5
  else
    return 1
  fi
}

kde_reader() {
  if command -v kreadconfig6 >/dev/null 2>&1; then
    printf '%s\n' kreadconfig6
  elif command -v kreadconfig5 >/dev/null 2>&1; then
    printf '%s\n' kreadconfig5
  else
    return 1
  fi
}

kde_proxy_is_enabled() {
  local reader proxy_type
  reader="$(kde_reader || true)"
  [[ -n "$reader" ]] || return 1
  proxy_type="$("$reader" --file kioslaverc --group 'Proxy Settings' --key ProxyType 2>/dev/null || true)"
  [[ -n "$proxy_type" && "$proxy_type" != "0" ]]
}

proxy_is_enabled() {
  is_managed_file "$shell_file" && return 0
  is_managed_file "$environment_file" && return 0
  gnome_proxy_is_enabled && return 0
  kde_proxy_is_enabled && return 0
  return 1
}

write_managed_files() {
  mkdir -p "$managed_dir" "$(dirname "$environment_file")"
  umask 077
  {
    printf '%s\n' '# Managed by Grouproxy proxy toggle.'
    printf 'export http_proxy=%q\n' "$proxy_url"
    printf 'export https_proxy=%q\n' "$proxy_url"
    printf 'export HTTP_PROXY=%q\n' "$proxy_url"
    printf 'export HTTPS_PROXY=%q\n' "$proxy_url"
    printf 'export no_proxy=%q\n' "$NO_PROXY_VALUE"
    printf 'export NO_PROXY=%q\n' "$NO_PROXY_VALUE"
  } > "$shell_file"
  {
    printf '%s\n' '# Managed by Grouproxy proxy toggle.'
    printf 'http_proxy=%s\n' "$proxy_url"
    printf 'https_proxy=%s\n' "$proxy_url"
    printf 'HTTP_PROXY=%s\n' "$proxy_url"
    printf 'HTTPS_PROXY=%s\n' "$proxy_url"
    printf 'no_proxy=%s\n' "$NO_PROXY_VALUE"
    printf 'NO_PROXY=%s\n' "$NO_PROXY_VALUE"
  } > "$environment_file"
  chmod 0600 "$shell_file" "$environment_file"
}

enable_gnome_proxy() {
  command -v gsettings >/dev/null 2>&1 || return 0
  if gsettings set org.gnome.system.proxy mode manual >/dev/null 2>&1 &&
    gsettings set org.gnome.system.proxy.http host "$PROXY_HOST" >/dev/null 2>&1 &&
    gsettings set org.gnome.system.proxy.http port "$PROXY_PORT" >/dev/null 2>&1 &&
    gsettings set org.gnome.system.proxy.https host "$PROXY_HOST" >/dev/null 2>&1 &&
    gsettings set org.gnome.system.proxy.https port "$PROXY_PORT" >/dev/null 2>&1; then
    gsettings set org.gnome.system.proxy ignore-hosts "['localhost', '127.0.0.1', '::1', '*.corp.internal', '${PROXY_HOST}']" >/dev/null 2>&1 || true
    printf 'GNOME proxy was enabled.\n'
  else
    printf 'GNOME settings are unavailable in this session; session files were still updated.\n' >&2
  fi
}

disable_gnome_proxy() {
  gnome_proxy_is_enabled || return 0
  if gsettings set org.gnome.system.proxy mode none >/dev/null 2>&1; then
    printf 'GNOME proxy was disabled.\n'
  else
    printf 'GNOME proxy could not be disabled in this session.\n' >&2
  fi
}

enable_kde_proxy() {
  local writer
  writer="$(kde_writer || true)"
  [[ -n "$writer" ]] || return 0
  if "$writer" --file kioslaverc --group 'Proxy Settings' --key ProxyType 1 &&
    "$writer" --file kioslaverc --group 'Proxy Settings' --key httpProxy "$PROXY_HOST $PROXY_PORT" &&
    "$writer" --file kioslaverc --group 'Proxy Settings' --key httpsProxy "$PROXY_HOST $PROXY_PORT" &&
    "$writer" --file kioslaverc --group 'Proxy Settings' --key NoProxyFor "$NO_PROXY_VALUE"; then
    printf 'KDE proxy was enabled.\n'
  else
    printf 'KDE proxy could not be enabled; session files were still updated.\n' >&2
  fi
}

disable_kde_proxy() {
  kde_proxy_is_enabled || return 0
  local writer
  writer="$(kde_writer || true)"
  [[ -n "$writer" ]] || return 0
  if "$writer" --file kioslaverc --group 'Proxy Settings' --key ProxyType 0 >/dev/null 2>&1; then
    printf 'KDE proxy was disabled.\n'
  else
    printf 'KDE proxy could not be disabled.\n' >&2
  fi
}

if proxy_is_enabled; then
  is_managed_file "$shell_file" && rm -f "$shell_file"
  is_managed_file "$environment_file" && rm -f "$environment_file"
  disable_gnome_proxy
  disable_kde_proxy
  printf 'Grouproxy proxy is now disabled. Run this same file again to enable it.\n'
else
  write_managed_files
  enable_gnome_proxy
  enable_kde_proxy
  printf 'Grouproxy proxy is now enabled at %s. Run this same file again to disable it.\n' "$proxy_url"
fi

printf 'Open a new terminal and restart affected applications to use the new state.\n'
