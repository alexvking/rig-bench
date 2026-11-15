# Rig Thermal & Fan Monitor

Lightweight, zero-dependency live **temperature & fan RPM** monitoring daemon + Web UI for Windows 10/11 desktops.

- **2-second continuous logging** to daily CSVs (`data/temperatures_YYYY-MM-DD.csv`) with unlimited retention
- **Dynamic hardware discovery** (`probe.ps1`) for any Ryzen/Intel CPU, NVIDIA/AMD GPU, motherboard Super I/O, NVMe/SATA drives, and USB/AIO fan controllers
- **AI-ready onboarding** via [`SKILL.md`](SKILL.md) — point an AI coding agent at this repo to probe sensors, map chassis/GPU fans, and start the dashboard
- **Fast, minimal Web UI** on `http://localhost:8095/` (and LAN IP) with `°C`/`°F`, `5m`–`All` windows, drag-to-zoom, click-to-toggle/solo, and `CSV` export

---

## Quick Start (AI-Assisted)

Open this directory in your AI coding tool (Antigravity, Claude Code, Cursor, etc.) and say:

> *"Follow `SKILL.md` to set up thermal and fan monitoring on this PC, identify all active fans, and start the dashboard."*

## Quick Start (Manual)

```powershell
# 1. Install/locate LibreHardwareMonitorLib + PawnIO + PowerShell 7
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_deps.ps1

# 2. Discover all temperature sensors, fans, and PWM controls (elevated)
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/probe.ps1

# 3. (Optional) Inspect or pulse fan headers to label chassis fans
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/identify_fans.ps1

# 4. Start background logger + Web UI (registers AutoStart at logon)
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/start_monitor.ps1
```

Open **http://localhost:8095/**.

To stop the service:
```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/stop_monitor.ps1
```

---

## RigBench Integration

- All configured temperature and fan channels are logged every 2s to `data/temperatures_YYYY-MM-DD.csv` (`FileShare.ReadWrite` safe for concurrent reads).
- HTTP JSON & CSV endpoints on port `8095`:
  - `GET /api/config` — active hardware summary and channel definitions
  - `GET /api/history?hours=24&maxPoints=1800` — downsampled peak-preserving series + min/avg/max stats
  - `GET /api/live?since=<ts_ms>` — incremental live samples
  - `GET /api/export.csv?hours=24` — CSV download (`hours=0` for all history)

## Files

- [`SKILL.md`](SKILL.md) — step-by-step AI agent setup & fan-mapping guide
- [`scripts/setup_deps.ps1`](scripts/setup_deps.ps1) — provisions `LibreHardwareMonitorLib.dll`, `PawnIO`, and `pwsh`
- [`scripts/probe.ps1`](scripts/probe.ps1) — probes hardware and generates `config/sensors.json`
- [`scripts/identify_fans.ps1`](scripts/identify_fans.ps1) — lists/pulses PWM headers to map physical fans
- [`scripts/thermal_service.ps1`](scripts/thermal_service.ps1) — dynamic C# + PowerShell 7 collector & HTTP server
- [`scripts/start_monitor.ps1`](scripts/start_monitor.ps1) / [`scripts/stop_monitor.ps1`](scripts/stop_monitor.ps1) — Scheduled Task lifecycle
- [`web/index.html`](web/index.html) — zero-dependency Canvas Web UI
- [`config/sensors.example.json`](config/sensors.example.json) — reference `config/sensors.json` schema
