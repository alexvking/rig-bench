#Requires -Version 5.1
<#
.SYNOPSIS
    Stops the RigThermalMonitor background daemon cleanly.
#>
$ErrorActionPreference = 'Continue'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    $exe = (Get-Process -Id $PID).Path
    $p = Start-Process -FilePath $exe -Verb RunAs -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"") -WindowStyle Hidden -Wait -PassThru
    exit $p.ExitCode
}

Stop-ScheduledTask -TaskName 'RigThermalMonitor AutoStart' -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'pwsh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'thermal_service\.ps1' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "[ok] Stopped RigThermalMonitor." -ForegroundColor Green
