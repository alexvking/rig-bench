<#
.SYNOPSIS
    Lists all Fan RPM & PWM Control sensors, or safely pulses a single Control header
    at 80% for 4 seconds (then restores default) to identify which physical fan it drives.
#>
[CmdletBinding()]
param(
    [string]$PulseControlId = '',
    [int]$PulsePct = 80,
    [int]$DurationSec = 4
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LibDir   = Join-Path $RepoRoot 'lib\LHM'
$lhmCandidates = @($LibDir, (Join-Path $env:USERPROFILE 'Tools\FanControl'), 'C:\Program Files\FanControl', 'C:\Program Files\LibreHardwareMonitor')
$LhmDir = $lhmCandidates | Where-Object { Test-Path (Join-Path $_ 'LibreHardwareMonitorLib.dll') } | Select-Object -First 1
if (-not $LhmDir) { throw "LibreHardwareMonitorLib.dll not found. Run scripts/setup_deps.ps1 first." }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $exe = (Get-Process -Id $PID).Path
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($PulseControlId) { $argList += @('-PulseControlId', $PulseControlId, '-PulsePct', $PulsePct, '-DurationSec', $DurationSec) }
    $p = Start-Process -FilePath $exe -Verb RunAs -ArgumentList $argList -Wait -PassThru
    exit $p.ExitCode
}

$resolveHandler = [System.ResolveEventHandler]{
    param($sender, $eventArgs)
    $asmShortName = (New-Object System.Reflection.AssemblyName($eventArgs.Name)).Name
    $candidate = Join-Path $LhmDir ($asmShortName + '.dll')
    if (Test-Path $candidate) { return [System.Reflection.Assembly]::LoadFrom($candidate) }
    return $null
}
[System.AppDomain]::CurrentDomain.add_AssemblyResolve($resolveHandler)
Add-Type -Path (Join-Path $LhmDir 'LibreHardwareMonitorLib.dll')

$comp = New-Object LibreHardwareMonitor.Hardware.Computer
$comp.IsMotherboardEnabled = $true
$comp.IsGpuEnabled = $true
$comp.IsControllerEnabled = $true
$comp.Open()

function Get-AllSensors($c) {
    $list = @()
    foreach ($hw in $c.Hardware) {
        $hw.Update()
        foreach ($s in $hw.Sensors) { $list += $s }
        foreach ($sub in $hw.SubHardware) {
            $sub.Update()
            foreach ($s in $sub.Sensors) { $list += $s }
        }
    }
    return $list
}

$all = Get-AllSensors $comp
Start-Sleep -Milliseconds 300
$all = Get-AllSensors $comp

if (-not $PulseControlId) {
    Write-Host "=== FAN RPM SENSORS ===" -ForegroundColor Cyan
    $all | Where-Object { $_.SensorType -eq 'Fan' } | ForEach-Object {
        Write-Host ("  {0,-36} | {1,-24} | {2,6:F0} RPM" -f $_.Identifier.ToString(), $_.Name, $_.Value)
    }
    Write-Host "`n=== PWM CONTROL SENSORS ===" -ForegroundColor Cyan
    $all | Where-Object { $_.SensorType -eq 'Control' } | ForEach-Object {
        Write-Host ("  {0,-36} | {1,-24} | {2,5:F1} %" -f $_.Identifier.ToString(), $_.Name, $_.Value)
    }
    $comp.Close()
    exit 0
}

$target = $all | Where-Object { $_.SensorType -eq 'Control' -and $_.Identifier.ToString() -eq $PulseControlId } | Select-Object -First 1
if (-not $target -or -not $target.Control) {
    $comp.Close()
    throw "Control sensor '$PulseControlId' not found or not controllable."
}

$before = @{}
$all | Where-Object { $_.SensorType -eq 'Fan' } | ForEach-Object { $before[$_.Identifier.ToString()] = [double]($_.Value ?? 0) }

try {
    Write-Host "Pulsing $PulseControlId ($($target.Name)) to $PulsePct% for ${DurationSec}s..." -ForegroundColor Yellow
    $target.Control.SetSoftware($PulsePct)
    Start-Sleep -Seconds $DurationSec
    $after = Get-AllSensors $comp
    Write-Host "=== RPM DELTAS ===" -ForegroundColor Cyan
    $after | Where-Object { $_.SensorType -eq 'Fan' } | ForEach-Object {
        $id = $_.Identifier.ToString()
        $b = $before[$id]
        $a = [double]($_.Value ?? 0)
        $delta = $a - $b
        $mark = if ([Math]::Abs($delta) -ge 120) { '<-- MATCH' } else { '' }
        Write-Host ("  {0,-34} | {1,-22} | {2,5:F0} -> {3,5:F0} RPM ({4:+0;-0;0}) {5}" -f $id, $_.Name, $b, $a, $delta, $mark)
    }
}
finally {
    try { $target.Control.SetDefault() } catch { }
    $comp.Close()
    Write-Host "Restored $PulseControlId to default control." -ForegroundColor Green
}
