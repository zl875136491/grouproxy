$ErrorActionPreference = "Stop"

# Run this file without parameters to toggle the current user's system proxy.
$ProxyHost = "test-proxy.1oa.com.cn"
$ProxyPort = 1080
$RegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"

New-Item -Path $RegistryPath -Force | Out-Null
$proxyEnabled = [int](Get-ItemPropertyValue -Path $RegistryPath -Name "ProxyEnable" -ErrorAction SilentlyContinue) -ne 0

if ($proxyEnabled) {
    New-ItemProperty -Path $RegistryPath -Name "ProxyEnable" -PropertyType DWord -Value 0 -Force | Out-Null
    $nextState = "disabled"
} else {
    New-ItemProperty -Path $RegistryPath -Name "ProxyServer" -PropertyType String -Value "${ProxyHost}:$ProxyPort" -Force | Out-Null
    New-ItemProperty -Path $RegistryPath -Name "ProxyEnable" -PropertyType DWord -Value 1 -Force | Out-Null
    $nextState = "enabled"
}

if (-not ("GrouproxyInternetOptions" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class GrouproxyInternetOptions {
    [DllImport("wininet.dll", SetLastError = true)]
    public static extern bool InternetSetOption(IntPtr handle, int option, IntPtr buffer, int bufferLength);
}
'@
}

[void][GrouproxyInternetOptions]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0)
[void][GrouproxyInternetOptions]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0)

if ($nextState -eq "enabled") {
    Write-Host "Grouproxy system proxy is enabled: ${ProxyHost}:$ProxyPort"
} else {
    Write-Host "Grouproxy system proxy is disabled."
}
Write-Host "Run this same file again to switch to the opposite state. Restart affected applications to use the new state."
