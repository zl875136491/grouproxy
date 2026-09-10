[CmdletBinding()]
param(
    [switch]$Disable
)

$ErrorActionPreference = "Stop"

# This minimal asset only switches the current user's Windows system proxy.
$ProxyHost = "test-proxy.1oa.com.cn"
$ProxyPort = 1080
$RegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"

New-Item -Path $RegistryPath -Force | Out-Null

if ($Disable) {
    New-ItemProperty -Path $RegistryPath -Name "ProxyEnable" -PropertyType DWord -Value 0 -Force | Out-Null
    Write-Host "Grouproxy system proxy is disabled."
    exit 0
}

New-ItemProperty -Path $RegistryPath -Name "ProxyServer" -PropertyType String -Value "${ProxyHost}:$ProxyPort" -Force | Out-Null
New-ItemProperty -Path $RegistryPath -Name "ProxyEnable" -PropertyType DWord -Value 1 -Force | Out-Null
Write-Host "Grouproxy system proxy is enabled: ${ProxyHost}:$ProxyPort"
