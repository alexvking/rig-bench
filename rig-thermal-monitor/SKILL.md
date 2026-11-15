---
name: rig-thermal-monitor
description: Auto-discover hardware temperature sensors, fan RPM headers, and PWM controls on any Windows desktop, configure config/sensors.json, and start the live thermal & fan monitoring dashboard on port 8095.
---

# Rig Thermal & Fan Monitor — AI Init & Onboarding Skill

Use this skill when setting up `rig-thermal-monitor` on a new Windows 10/11 PC (e.g., AMD Ryzen 3000/5000/7000/9000, Intel Core, NVIDIA RTX 30/40/50 series, AMD Radeon, AIO/USB fan controllers).

## Workflow

### 1. Provision Dependencies
Run `scripts/setup_deps.ps1` (works in Windows PowerShell 5.1 or PowerShell 7):
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_deps.ps1
```
This ensures:
- PowerShell 7 (`pwsh`) is installed.
- `LibreHardwareMonitorLib.dll` (+ dependencies) is placed in `lib/LHM/` (copied from FanControl/LHM if present, or downloaded from GitHub).
- `PawnIO` signed kernel sensor driver is installed (required on Windows 11 HVCI for Ryzen SMU & Super I/O sensors).

### 2. Discover Hardware, Temperatures & Fans (Elevated)
Run the hardware probe as Administrator:
```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/probe.ps1
```
This writes:
- `data/discovered_sensors.json`: complete dump of every `Temperature`, `Fan`, `Control`, `Power`, and `Load` sensor.
- `config/sensors.json`: auto-generated dashboard & logger configuration.

### 3. Inspect & Refine `config/sensors.json`
Read `data/discovered_sensors.json` and `config/sensors.json`:
1. **Verify Temperatures:**
   - Check that CPU (`Tctl/Tdie`, `CCD1`, `CCD2`, or `CPU Package`), GPU (`GPU Core`, `GPU Hot Spot`, `GPU Memory Junction` if present), Motherboard (`SYSTIN`/`Motherboard`, `CPUTIN`/`CPU`, `VRM`), and all NVMe/SATA drives are present and `enabled: true`.
   - Keep unwired Super I/O pins (`0 °C`, `127 °C`, or static `Auxiliary` pins) as `enabled: false` so they stay out of the main grid (still accessible in the bottom pill bar).
2. **Verify & Address All Fans:**
   - Check `fans` in `config/sensors.json` and `Control` sensors in `data/discovered_sensors.json`.
   - **Zero-RPM GPU Fans (common on RTX 4080 / 4090 / 30-series):** Modern GPUs turn off fans below ~50 °C at idle, so `probe.ps1` may mark `/gpu-nvidia/0/fan/1` (and `/fan/2`, `/fan/3`) as `enabled: false`. **Set `enabled: true` for all GPU fans** so they appear on the chart as soon as a RigBench/gaming load spins them up.
   - **Unlabeled Motherboard / Chassis Headers (`Chassis #1..N`, `Fan #1..N`):**
     - Any `0 RPM` header with nothing plugged in should remain `enabled: false` (or be removed).
     - If the user wants to know which physical fan (`Front Intake`, `Rear Exhaust`, `Top AIO`) corresponds to which `Fan #N` / `Chassis #N` header, use `scripts/identify_fans.ps1`:
       ```powershell
       pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/identify_fans.ps1
       pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/identify_fans.ps1 -PulseControlId "/lpc/nct6798d/0/control/2"
       ```
       Ask the user which fan spun up, or match the RPM delta, then update `label` in `config/sensors.json`.

### 4. Start & Verify the Service
Start the background daemon and register the `RigThermalMonitor AutoStart` Scheduled Task:
```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/start_monitor.ps1
```
Wait ~4 seconds and verify:
1. `data/service_status.json` shows `"status": "running"` and `"isAdmin": true`.
2. `data/temperatures_YYYY-MM-DD.csv` is actively appending rows every 2 seconds with non-null CPU, GPU, Motherboard, Storage, and Fan values.
3. `http://localhost:8095/api/config` and `http://localhost:8095/` respond with HTTP 200.

### 5. RigBench Integration
- `rig-thermal-monitor` runs continuously on `http://localhost:8095/` (and LAN IP `:8095`).
- During or after a RigBench run, query `/api/history?hours=1` or export synchronized 2-second CSV telemetry via `GET http://localhost:8095/api/export.csv?hours=1` (or read `data/temperatures_YYYY-MM-DD.csv` directly — opened with `FileShare.ReadWrite`).
