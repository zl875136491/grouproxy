[CmdletBinding()]
param(
    [switch]$Disable,
    [switch]$SkipDirectTest
)

$ErrorActionPreference = "Stop"

# This file is a pre-generated access asset. It configures the current Windows
# user only and never installs proxy credentials or a CA certificate.
# Run the same file with -Disable to restore the saved settings.
$ProxyHost = "test-proxy.1oa.com.cn"
$ProxyPort = 1080
$ProxyUrl = "http://${ProxyHost}:$ProxyPort"
$RegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
$StateRoot = Join-Path $env:LOCALAPPDATA "Grouproxy"
$StatePath = Join-Path $StateRoot "proxy-backup.json"

function Get-OptionalRegistryValue([string]$Name) {
    try {
        return Get-ItemPropertyValue -Path $RegistryPath -Name $Name -ErrorAction Stop
    } catch {
        return $null
    }
}

function Save-ProxyState {
    if (Test-Path -LiteralPath $StatePath) { return }
    New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
    $state = [ordered]@{
        ProxyEnable = Get-OptionalRegistryValue "ProxyEnable"
        ProxyServer = Get-OptionalRegistryValue "ProxyServer"
        ProxyOverride = Get-OptionalRegistryValue "ProxyOverride"
        HttpProxy = [Environment]::GetEnvironmentVariable("HTTP_PROXY", "User")
        HttpsProxy = [Environment]::GetEnvironmentVariable("HTTPS_PROXY", "User")
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
}

function Set-OptionalRegistryValue([string]$Name, $Value, [switch]$DWord) {
    if ($null -eq $Value) {
        Remove-ItemProperty -Path $RegistryPath -Name $Name -ErrorAction SilentlyContinue
        return
    }
    if ($DWord) {
        New-ItemProperty -Path $RegistryPath -Name $Name -PropertyType DWord -Value ([int]$Value) -Force | Out-Null
    } else {
        New-ItemProperty -Path $RegistryPath -Name $Name -PropertyType String -Value ([string]$Value) -Force | Out-Null
    }
}

function Refresh-WinInet {
    if (-not ("Grouproxy.WinInet" -as [type])) {
        Add-Type @"
using System;
using System.Runtime.InteropServices;
namespace Grouproxy {
    public static class WinInet {
        [DllImport("wininet.dll", SetLastError = true)]
        public static extern bool InternetSetOption(IntPtr hInternet, int dwOption, IntPtr lpBuffer, int dwBufferLength);
    }
}
"@
    }
    [Grouproxy.WinInet]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0) | Out-Null
    [Grouproxy.WinInet]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0) | Out-Null
}

function Restore-ProxyState {
    if (-not (Test-Path -LiteralPath $StatePath)) {
        Set-OptionalRegistryValue "ProxyEnable" 0 -DWord
        Write-Host "No Grouproxy backup was found; the current-user proxy was disabled."
        return
    }
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    Set-OptionalRegistryValue "ProxyEnable" $state.ProxyEnable -DWord
    Set-OptionalRegistryValue "ProxyServer" $state.ProxyServer
    Set-OptionalRegistryValue "ProxyOverride" $state.ProxyOverride
    [Environment]::SetEnvironmentVariable("HTTP_PROXY", $state.HttpProxy, "User")
    [Environment]::SetEnvironmentVariable("HTTPS_PROXY", $state.HttpsProxy, "User")
    Remove-Item -LiteralPath $StatePath -Force
    Write-Host "Restored the previous Windows proxy settings. New processes may need to be restarted."
}

function Invoke-DirectChecks {
    Write-Host ""
    Write-Host "--- Direct connection checks ---" -ForegroundColor Yellow
    try {
        $directIp = Invoke-RestMethod -Uri "https://ipinfo.io/json" -TimeoutSec 5
        Write-Host "Direct IP: $($directIp.ip) | Country: $($directIp.country) | Organization: $($directIp.org)"
    } catch {
        Write-Host "Unable to read the direct IP (timeout or blocked network)." -ForegroundColor Red
    }
    try {
        $response = Invoke-WebRequest -Uri "https://www.google.com" -TimeoutSec 5 -UseBasicParsing
        Write-Host "[OK] Direct Google request -> HTTP $($response.StatusCode)" -ForegroundColor Green
    } catch {
        Write-Host "[BLOCKED] Direct Google request" -ForegroundColor Red
    }
}

if ($Disable) {
    Restore-ProxyState
    Refresh-WinInet
    exit 0
}

Save-ProxyState
New-Item -Path $RegistryPath -Force | Out-Null
New-ItemProperty -Path $RegistryPath -Name "ProxyEnable" -PropertyType DWord -Value 1 -Force | Out-Null
Set-ItemProperty -Path $RegistryPath -Name "ProxyServer" -Value "${ProxyHost}:$ProxyPort"
Set-ItemProperty -Path $RegistryPath -Name "ProxyOverride" -Value "<local>;localhost;127.0.0.1;*.corp.internal"
[Environment]::SetEnvironmentVariable("HTTP_PROXY", $ProxyUrl, "User")
[Environment]::SetEnvironmentVariable("HTTPS_PROXY", $ProxyUrl, "User")
$env:HTTP_PROXY = $ProxyUrl
$env:HTTPS_PROXY = $ProxyUrl
Refresh-WinInet

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "Grouproxy proxy: $ProxyUrl" -ForegroundColor Cyan
Write-Host "Current-user Windows proxy settings are enabled." -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Cyan

if (-not $SkipDirectTest) {
    $choice = Read-Host "Run direct-network checks first? [y/N]"
    if ($choice -match "^[Yy]$") { Invoke-DirectChecks }
}

Write-Host ""
Write-Host "--- Proxy checks ---" -ForegroundColor Yellow
$proxyFailures = 0
try {
    $proxyIp = Invoke-RestMethod -Uri "https://ipinfo.io/json" -Proxy $ProxyUrl -TimeoutSec 6
    Write-Host "Proxy IP: $($proxyIp.ip) | Country: $($proxyIp.country) | Organization: $($proxyIp.org)" -ForegroundColor Green
} catch {
    $proxyFailures++
    Write-Host "[FAILED] Proxy IP request: $($_.Exception.Message)" -ForegroundColor Red
}

foreach ($site in @("https://www.google.com", "https://www.youtube.com", "https://github.com")) {
    try {
        $response = Invoke-WebRequest -Uri $site -Proxy $ProxyUrl -TimeoutSec 6 -UseBasicParsing
        Write-Host "[OK] $site -> HTTP $($response.StatusCode)" -ForegroundColor Green
    } catch {
        $proxyFailures++
        Write-Host "[FAILED] $site -> $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host "==========================================" -ForegroundColor Cyan
if ($proxyFailures -gt 0) {
    Write-Host "Proxy configuration is enabled, but one or more checks failed." -ForegroundColor Yellow
    exit 1
}
Write-Host "Proxy configuration and checks completed successfully." -ForegroundColor Green
