<#
.SYNOPSIS
    Elevated Hardware, Temperature, Fan & PWM Control Discovery Script.
    Writes data/discovered_sensors.json and generates config/sensors.json.
#>
[CmdletBinding()]
param(
    [switch]$ForceConfig,
    [int]$Port = 8095,
    [int]$IntervalMs = 2000
)

$ErrorActionPreference = 'Stop'
[System.Globalization.CultureInfo]::CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture

$RepoRoot  = Split-Path -Parent $PSScriptRoot
$DataDir   = Join-Path $RepoRoot 'data'
$ConfigDir = Join-Path $RepoRoot 'config'
$LibDir    = Join-Path $RepoRoot 'lib\LHM'

New-Item -ItemType Directory -Force -Path $DataDir, $ConfigDir | Out-Null

# Self-elevate if not Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    $exe = (Get-Process -Id $PID).Path
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Port', $Port, '-IntervalMs', $IntervalMs)
    if ($ForceConfig) { $argList += '-ForceConfig' }
    $p = Start-Process -FilePath $exe -Verb RunAs -ArgumentList $argList -Wait -PassThru
    exit $p.ExitCode
}

# Locate LibreHardwareMonitorLib.dll
$lhmCandidates = @(
    $LibDir,
    (Join-Path $env:USERPROFILE 'Tools\FanControl'),
    'C:\Program Files\FanControl',
    'C:\Program Files (x86)\FanControl',
    'C:\Program Files\LibreHardwareMonitor'
)
$LhmDir = $lhmCandidates | Where-Object { Test-Path (Join-Path $_ 'LibreHardwareMonitorLib.dll') } | Select-Object -First 1
if (-not $LhmDir) {
    throw "LibreHardwareMonitorLib.dll not found. Run scripts/setup_deps.ps1 first."
}
$LhmDll = Join-Path $LhmDir 'LibreHardwareMonitorLib.dll'

$resolveHandler = [System.ResolveEventHandler]{
    param($sender, $eventArgs)
    $asmShortName = (New-Object System.Reflection.AssemblyName($eventArgs.Name)).Name
    $candidate = Join-Path $LhmDir ($asmShortName + '.dll')
    if (Test-Path $candidate) { return [System.Reflection.Assembly]::LoadFrom($candidate) }
    return $null
}
[System.AppDomain]::CurrentDomain.add_AssemblyResolve($resolveHandler)
Add-Type -Path $LhmDll

$comp = New-Object LibreHardwareMonitor.Hardware.Computer
$comp.IsCpuEnabled         = $true
$comp.IsGpuEnabled         = $true
$comp.IsMotherboardEnabled = $true
$comp.IsStorageEnabled     = $true
$comp.IsControllerEnabled  = $true
$comp.IsMemoryEnabled      = $true
$comp.IsPsuEnabled         = $true
$comp.Open()

# Two update passes so delta-based power/RPM/clock sensors settle
function Update-All($c) {
    foreach ($hw in $c.Hardware) {
        $hw.Update()
        foreach ($sub in $hw.SubHardware) { $sub.Update() }
    }
}
Update-All $comp
Start-Sleep -Milliseconds 350
Update-All $comp

$sensors = [System.Collections.Generic.List[object]]::new()
$hwList  = [System.Collections.Generic.List[object]]::new()

foreach ($hw in $comp.Hardware) {
    $hwList.Add([pscustomobject][ordered]@{
        type = $hw.HardwareType.ToString()
        name = $hw.Name
        id   = $hw.Identifier.ToString()
    })
    foreach ($s in $hw.Sensors) {
        if ($s.SensorType -in @('Temperature', 'Fan', 'Control', 'Power', 'Load')) {
            $sensors.Add([pscustomobject][ordered]@{
                hwType     = $hw.HardwareType.ToString()
                hwName     = $hw.Name
                subName    = ''
                sensorType = $s.SensorType.ToString()
                name       = $s.Name
                id         = $s.Identifier.ToString()
                value      = if ($null -ne $s.Value -and -not [double]::IsNaN($s.Value)) { [Math]::Round([double]$s.Value, 2) } else { $null }
            })
        }
    }
    foreach ($sub in $hw.SubHardware) {
        $hwList.Add([pscustomobject][ordered]@{
            type = $sub.HardwareType.ToString()
            name = $sub.Name
            id   = $sub.Identifier.ToString()
        })
        foreach ($s in $sub.Sensors) {
            if ($s.SensorType -in @('Temperature', 'Fan', 'Control', 'Power', 'Load')) {
                $sensors.Add([pscustomobject][ordered]@{
                    hwType     = $sub.HardwareType.ToString()
                    hwName     = $hw.Name
                    subName    = $sub.Name
                    sensorType = $s.SensorType.ToString()
                    name       = $s.Name
                    id         = $s.Identifier.ToString()
                    value      = if ($null -ne $s.Value -and -not [double]::IsNaN($s.Value)) { [Math]::Round([double]$s.Value, 2) } else { $null }
                })
            }
        }
    }
}
$comp.Close()

# Host hardware summary
$cpuName = ($hwList | Where-Object { $_.type -eq 'Cpu' } | Select-Object -First 1 -ExpandProperty name)
$gpuNames = @($hwList | Where-Object { $_.type -match '^Gpu' } | Select-Object -ExpandProperty name)
$mbName = ($hwList | Where-Object { $_.type -eq 'Motherboard' } | Select-Object -First 1 -ExpandProperty name)
$storageList = @($hwList | Where-Object { $_.type -eq 'Storage' })

$summaryParts = @()
if ($cpuName) { $summaryParts += ($cpuName -replace 'AMD |Intel\(R\) Core\(TM\) | Processor', '').Trim() }
foreach ($g in $gpuNames) { $summaryParts += ($g -replace 'NVIDIA GeForce |AMD Radeon ', '').Trim() }
if ($mbName) { $summaryParts += ($mbName -replace 'ASRock |ASUS |MSI |Gigabyte ', '').Trim() }
if ($storageList.Count -gt 0) { $summaryParts += ($storageList.Count -gt 1 ? "$($storageList.Count)x Storage" : 'NVMe/SSD') }
$hwSummary = ($summaryParts -join ' · ')

$discoveredPath = Join-Path $DataDir 'discovered_sensors.json'
$discoveredDoc = [ordered]@{
    probedAt   = [DateTimeOffset]::Now.ToString('o')
    isAdmin    = $isAdmin
    lhmDir     = $LhmDir
    host       = [ordered]@{
        name        = $env:COMPUTERNAME
        cpu         = $cpuName
        gpus        = $gpuNames
        motherboard = $mbName
        storage     = @($storageList | Select-Object -ExpandProperty name)
        summary     = $hwSummary
    }
    hardware   = $hwList
    sensors    = $sensors
}
$discoveredDoc | ConvertTo-Json -Depth 6 | Set-Content -Path $discoveredPath -Encoding UTF8
Write-Host "[ok] Discovered $($sensors.Count) sensors -> $discoveredPath" -ForegroundColor Green

# Auto-generate config/sensors.json
$configPath = Join-Path $ConfigDir 'sensors.json'
if ((Test-Path $configPath) -and -not $ForceConfig) {
    Write-Host "[info] Keeping existing $configPath (pass -ForceConfig to regenerate)." -ForegroundColor Yellow
    exit 0
}

$tempPalette = @('#1dd1a1','#ff6b6b','#ff9f43','#feca57','#54a0ff','#a29bfe','#48dbfb','#f368e0','#00d2d3','#ff9ff3','#c8d6e5')
$fanPalette  = @('#38bdf8','#34d399','#fbbf24','#a78bfa','#f472b6','#fb7185','#2dd4bf','#60a5fa')
$usedKeys    = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)

function Make-Key([string]$raw) {
    $k = ($raw.ToLowerInvariant() -replace '[^a-z0-9]+', '_').Trim('_')
    if (-not $k) { $k = 'sensor' }
    $base = $k
    $n = 2
    while ($usedKeys.Contains($k)) { $k = "${base}_${n}"; $n++ }
    [void]$usedKeys.Add($k)
    return $k
}

$tempsOut = [System.Collections.Generic.List[object]]::new()
$fansOut  = [System.Collections.Generic.List[object]]::new()
$tColorIdx = 0
$fColorIdx = 0

$storageCount = ($sensors | Where-Object { $_.sensorType -eq 'Temperature' -and $_.hwType -eq 'Storage' -and $_.name -notmatch 'Warning|Critical' }).Count

foreach ($s in $sensors) {
    if ($s.sensorType -eq 'Temperature') {
        if ($s.name -match 'Warning|Critical|Limit|Distance to TjMax') { continue }
        if ($s.id -match '/nvme/\d+/temperature/1[01]$') { continue }

        $val = $s.value
        $label = $s.name
        $tag   = if ($s.subName) { $s.subName } else { $s.hwName }
        $warn  = 80
        $crit  = 92
        $primary = $true
        $enabled = $true

        if ($s.hwType -eq 'Cpu') {
            $tag = 'CPU'
            if ($s.name -match 'CCD\s*(\d+)') { $label = "CPU CCD$($Matches[1])"; $tag = 'Zen Die' }
            elseif ($s.name -match 'Tctl|Tdie') { $label = 'CPU Die'; $tag = 'Tctl/Tdie' }
            elseif ($s.name -match 'Package') { $label = 'CPU Package'; $tag = 'Package' }
            elseif ($s.name -match 'Core Max') { $label = 'CPU Core Max'; $tag = 'Max Core' }
            elseif ($s.name -match 'Core #\d+') { $primary = $false; $enabled = $false }
        }
        elseif ($s.hwType -eq 'SuperIO') {
            $tag = ($s.subName -replace 'Nuvoton |ITE |Fintek ', '').Trim()
            if ($s.name -eq 'Motherboard' -or $s.name -eq 'SYSTIN' -or $s.name -eq 'System') {
                $label = 'Motherboard'; $warn = 55; $crit = 65
            } elseif ($s.name -eq 'CPU' -or $s.name -eq 'CPUTIN') {
                $label = 'CPU Socket'; $warn = 70; $crit = 82
            } elseif ($s.name -match 'VRM|MOS') {
                $label = $s.name; $warn = 85; $crit = 100
                if ($null -eq $val -or $val -le 5 -or $val -ge 115) { $primary = $false; $enabled = $false }
            } elseif ($s.name -match 'Chipset|PCH') {
                $label = 'Chipset'; $warn = 75; $crit = 90
            } elseif ($s.name -match 'Auxiliary|TMPIN|Sensor') {
                $label = $s.name -replace 'Auxiliary Index #', 'Aux #' -replace 'Auxiliary', 'Aux #1'
                $primary = $false; $enabled = $false; $warn = 999; $crit = 999
            } else {
                if ($null -eq $val -or $val -le 0 -or $val -ge 115) { $primary = $false; $enabled = $false }
            }
        }
        elseif ($s.hwType -match '^Gpu') {
            $shortGpu = ($s.hwName -replace 'NVIDIA GeForce |AMD Radeon ', '').Trim()
            $tag = $shortGpu
            if ($s.name -eq 'GPU Core') { $label = 'GPU Core'; $warn = 78; $crit = 87 }
            elseif ($s.name -match 'Hot Spot') { $label = 'GPU Hot Spot'; $warn = 88; $crit = 96 }
            elseif ($s.name -match 'Memory') { $label = 'GPU VRAM'; $warn = 95; $crit = 105 }
            else { $warn = 85; $crit = 95 }
        }
        elseif ($s.hwType -eq 'Storage') {
            $shortDisk = ($s.hwName -replace 'Samsung SSD |WD_BLACK |Crucial |SPCC |M\.2 PCIE SSD', '').Trim()
            if (-not $shortDisk) { $shortDisk = 'Storage' }
            $tag = $s.hwName
            $label = if ($storageCount -gt 1) { "SSD $shortDisk" } else { 'NVMe SSD' }
            $warn = 70; $crit = 82
        }
        else {
            if ($null -eq $val -or $val -le 0 -or $val -ge 115) { $primary = $false; $enabled = $false }
        }

        $color = if ($primary) { $tempPalette[$tColorIdx % $tempPalette.Count]; $tColorIdx++ } else { '#94a3b8' }
        $tempsOut.Add([ordered]@{
            key     = Make-Key $label
            label   = $label
            tag     = $tag
            id      = $s.id
            color   = $color
            warn    = $warn
            crit    = $crit
            primary = $primary
            enabled = $enabled
        })
    }
    elseif ($s.sensorType -eq 'Fan') {
        $isActive = ($null -ne $s.value -and $s.value -gt 0)
        $tag = if ($s.hwType -match '^Gpu') { ($s.hwName -replace 'NVIDIA GeForce |AMD Radeon ', '').Trim() }
               elseif ($s.subName) { ($s.subName -replace 'Nuvoton |ITE ', '').Trim() }
               else { $s.hwName }
        $label = $s.name
        if ($s.hwType -match '^Gpu' -and $label -notmatch 'GPU') { $label = "GPU $label" }

        $color = $fanPalette[$fColorIdx % $fanPalette.Count]
        if ($isActive) { $fColorIdx++ }

        $fanObj = [ordered]@{
            key     = Make-Key $label
            label   = $label
            tag     = $tag
            id      = $s.id
            color   = $color
            primary = $true
            enabled = $isActive
        }
        if (-not $isActive) {
            $fanObj['note'] = '0 RPM at probe (empty header or zero-RPM idle mode)'
        }
        $fansOut.Add($fanObj)
    }
}

# Pick supporting power/load metrics
$cpuW   = ($sensors | Where-Object { $_.hwType -eq 'Cpu' -and $_.sensorType -eq 'Power' -and $_.name -match 'Package' } | Select-Object -First 1 -ExpandProperty id)
$cpuPct = ($sensors | Where-Object { $_.hwType -eq 'Cpu' -and $_.sensorType -eq 'Load' -and $_.name -eq 'CPU Total' } | Select-Object -First 1 -ExpandProperty id)
$gpuW   = ($sensors | Where-Object { $_.hwType -match '^Gpu' -and $_.sensorType -eq 'Power' -and $_.name -match 'Package|Core|Board' } | Select-Object -First 1 -ExpandProperty id)
$gpuPct = ($sensors | Where-Object { $_.hwType -match '^Gpu' -and $_.sensorType -eq 'Load' -and $_.name -eq 'GPU Core' } | Select-Object -First 1 -ExpandProperty id)

$configDoc = [ordered]@{
    host    = [ordered]@{
        name       = $env:COMPUTERNAME
        summary    = $hwSummary
        port       = $Port
        intervalMs = $IntervalMs
    }
    temps   = $tempsOut
    fans    = $fansOut
    metrics = [ordered]@{
        cpu_w   = $cpuW
        cpu_pct = $cpuPct
        gpu_w   = $gpuW
        gpu_pct = $gpuPct
    }
}

$configDoc | ConvertTo-Json -Depth 6 | Set-Content -Path $configPath -Encoding UTF8
Write-Host "[ok] Generated $configPath ($($tempsOut.Count) temp channels, $($fansOut.Count) fan channels)" -ForegroundColor Green

Write-Host "`n--- ACTIVE TEMPERATURES ---" -ForegroundColor Cyan
$tempsOut | Where-Object { $_.enabled } | ForEach-Object { Write-Host ("  {0,-16} | {1,-32} | {2}" -f $_.label, $_.id, $_.tag) }
Write-Host "`n--- ACTIVE FANS (>0 RPM) ---" -ForegroundColor Cyan
$fansOut | Where-Object { $_.enabled } | ForEach-Object { Write-Host ("  {0,-16} | {1,-32} | {2}" -f $_.label, $_.id, $_.tag) }
Write-Host "`n--- 0-RPM / DISABLED FAN HEADERS ---" -ForegroundColor DarkGray
$fansOut | Where-Object { -not $_.enabled } | ForEach-Object { Write-Host ("  {0,-16} | {1,-32} | {2}" -f $_.label, $_.id, $_.tag) }
