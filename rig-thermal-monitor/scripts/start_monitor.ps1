#Requires -Version 5.1
<#
.SYNOPSIS
    Registers/updates the 'RigThermalMonitor AutoStart' Windows Scheduled Task (-RunLevel Highest)
    and starts the background monitoring daemon immediately.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
$PwshExe = if ($pwshCmd) { $pwshCmd.Source } elseif (Test-Path 'C:\Program Files\PowerShell\7\pwsh.exe') { 'C:\Program Files\PowerShell\7\pwsh.exe' } else { throw "PowerShell 7 (pwsh.exe) is required. Run scripts/setup_deps.ps1 first." }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    $p = Start-Process -FilePath $PwshExe -Verb RunAs -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"") -WindowStyle Hidden -Wait -PassThru
    exit $p.ExitCode
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$TaskName = 'RigThermalMonitor AutoStart'
$SvcPath  = Join-Path $PSScriptRoot 'thermal_service.ps1'

$action    = New-ScheduledTaskAction -Execute $PwshExe -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$SvcPath`" -NoElevate" -WorkingDirectory $RepoRoot
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval ([TimeSpan]::FromMinutes(1))
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'pwsh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'thermal_service\.ps1' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 300

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "[ok] Started '$TaskName'." -ForegroundColor Green
