<#
.SYNOPSIS
    Ensures LibreHardwareMonitorLib.dll, PawnIO (Windows 11 HVCI-compatible Ring-0 sensor driver),
    and PowerShell 7 (pwsh) are available for rig-thermal-monitor.
#>
[CmdletBinding()]
param(
    [switch]$ForceDownloadLhm
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LibDir   = Join-Path $RepoRoot 'lib\LHM'
$LhmDll   = Join-Path $LibDir 'LibreHardwareMonitorLib.dll'

New-Item -ItemType Directory -Force -Path $LibDir | Out-Null

function Write-Info($msg) { Write-Host "[setup] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[ok]    $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[warn]  $msg" -ForegroundColor Yellow }

# 1. Check PowerShell 7 (pwsh)
$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
if ($pwsh) {
    Write-Ok "PowerShell 7 found: $($pwsh.Source)"
} else {
    Write-Warn "PowerShell 7 (pwsh) not found. Attempting install via winget..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install --id Microsoft.PowerShell --source winget --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Installed PowerShell 7."
        } else {
            Write-Warn "winget install returned $LASTEXITCODE. Install PowerShell 7 manually: https://aka.ms/powershell"
        }
    } else {
        Write-Warn "winget not available. Install PowerShell 7 manually: https://aka.ms/powershell"
    }
}

# 2. Locate or download LibreHardwareMonitorLib.dll
if (-not $ForceDownloadLhm -and -not (Test-Path $LhmDll)) {
    $candidates = @(
        (Join-Path $env:USERPROFILE 'Tools\FanControl'),
        'C:\Program Files\FanControl',
        'C:\Program Files (x86)\FanControl',
        'C:\Program Files\LibreHardwareMonitor',
        (Join-Path $env:LOCALAPPDATA 'FanControl'),
        (Join-Path $env:USERPROFILE 'Tools\LibreHardwareMonitor')
    )
    foreach ($dir in $candidates) {
        $candDll = Join-Path $dir 'LibreHardwareMonitorLib.dll'
        if (Test-Path $candDll) {
            Write-Info "Copying LibreHardwareMonitorLib + dependencies from $dir -> $LibDir"
            Get-ChildItem -Path $dir -Filter '*.dll' | Copy-Item -Destination $LibDir -Force
            $pawnCand = Join-Path $dir 'PawnIO_setup.exe'
            if (Test-Path $pawnCand) {
                Copy-Item -Path $pawnCand -Destination $LibDir -Force
            }
            break
        }
    }
}

if ($ForceDownloadLhm -or -not (Test-Path $LhmDll)) {
    Write-Info "Downloading latest LibreHardwareMonitor release from GitHub..."
    $headers = @{ 'User-Agent' = 'rig-thermal-monitor-setup' }
    $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest' -Headers $headers
    $asset = $rel.assets | Where-Object { $_.name -match 'LibreHardwareMonitor\.zip$|net472\.zip$' } | Select-Object -First 1
    if (-not $asset) {
        $asset = $rel.assets | Where-Object { $_.name -match '\.zip$' } | Select-Object -First 1
    }
    if (-not $asset) {
        throw "Could not find a .zip asset in LibreHardwareMonitor latest release."
    }
    $zipPath = Join-Path $env:TEMP $asset.name
    $extDir  = Join-Path $env:TEMP ('LHM_extract_' + [guid]::NewGuid().ToString('N'))
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -Headers $headers
    Expand-Archive -Path $zipPath -DestinationPath $extDir -Force
    Get-ChildItem -Path $extDir -Filter '*.dll' -Recurse | Copy-Item -Destination $LibDir -Force
    $pawnInZip = Get-ChildItem -Path $extDir -Filter 'PawnIO_setup.exe' -Recurse | Select-Object -First 1
    if ($pawnInZip) {
        Copy-Item -Path $pawnInZip.FullName -Destination $LibDir -Force
    }
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
    Remove-Item $extDir -Recurse -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path $LhmDll)) {
    throw "Failed to provision LibreHardwareMonitorLib.dll into $LibDir"
}

Get-ChildItem -Path $LibDir -Filter '*.dll' | Unblock-File -ErrorAction SilentlyContinue
Write-Ok "LibreHardwareMonitorLib.dll ready at $LhmDll"

# 3. Ensure PawnIO driver is installed (needed on Win11 HVCI for Ryzen SMU & Super I/O)
$pawnInstalled = (Test-Path "$env:SystemRoot\System32\drivers\PawnIO.sys") -or
                 ($null -ne (Get-Service -Name 'PawnIO' -ErrorAction SilentlyContinue))
if ($pawnInstalled) {
    Write-Ok "PawnIO kernel sensor driver is already installed."
} else {
    $pawnSetup = Join-Path $LibDir 'PawnIO_setup.exe'
    if (-not (Test-Path $pawnSetup)) {
        Write-Info "Downloading PawnIO_setup.exe from GitHub (namazso/PawnIO.Setup)..."
        try {
            $headers = @{ 'User-Agent' = 'rig-thermal-monitor-setup' }
            $pRel = Invoke-RestMethod -Uri 'https://api.github.com/repos/namazso/PawnIO.Setup/releases/latest' -Headers $headers
            $pAsset = $pRel.assets | Where-Object { $_.name -match 'PawnIO_setup\.exe$' } | Select-Object -First 1
            if ($pAsset) {
                Invoke-WebRequest -Uri $pAsset.browser_download_url -OutFile $pawnSetup -Headers $headers
            }
        } catch {
            Write-Warn "Could not download PawnIO_setup.exe automatically: $($_.Exception.Message)"
        }
    }

    if (Test-Path $pawnSetup) {
        Unblock-File -Path $pawnSetup -ErrorAction SilentlyContinue
        Write-Info "Installing PawnIO sensor driver silently (UAC elevation may prompt)..."
        $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator
        )
        $proc = if ($isAdmin) {
            Start-Process -FilePath $pawnSetup -ArgumentList '-install -silent' -Wait -PassThru -NoNewWindow
        } else {
            Start-Process -FilePath $pawnSetup -ArgumentList '-install -silent' -Verb RunAs -Wait -PassThru
        }
        if ($proc.ExitCode -eq 0) {
            Write-Ok "PawnIO installed."
        } else {
            Write-Warn "PawnIO_setup.exe exited with code $($proc.ExitCode)."
        }
    } else {
        Write-Warn "PawnIO_setup.exe not found. CPU/SuperIO sensors may require running PawnIO installer manually."
    }
}
