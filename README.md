# rig-bench

Cross-platform CLI benchmark harness and interactive ECharts Web UI for comparing **Windows (AMD AM4/AM5, Intel, NVIDIA CUDA / Vulkan)**, **macOS (Apple Silicon)**, and **Linux** systems across CPU, GPU, LLM, out-of-core DuckDB/NVMe compute, and continuous thermal telemetry.

## What It Measures (22 Core Metrics + Live Thermals)

* **CPU Compute**: 7-Zip Single-Thread / Multi-Thread MIPS (plus measured peak 1T MHz), NumPy Dense FP64 GEMM (GFLOPS), parallel SHA-256 Single-Core / All-Core (MB/s), and optional Geekbench 6.4 / Cinebench 2024.
* **GPU Graphics and Math**: Blender EEVEE 1440p raster, Blender Cycles GPU path tracing (CUDA / MetalRT / HIP), PyTorch FP32/FP16 GEMM + 2D Complex FFT + Memory Triad Bandwidth, and `llama-bench` Qwen2.5-7B (`pp512` / `tg128`).
* **SSD and Out-of-Core Compute**: `fio` uncached direct NVMe (`1M QD8` seq, `4K QD1` latency, `4K QD32` IOPS) + 80M-row DuckDB `4 GB`-capped out-of-core Parquet ingest, spill join, and `zstd` parallel recompression.
* **Continuous Thermal Telemetry (`runners/thermals.py`)**: Background 4-second polling of CPU (`Tctl/Tdie` / Package via `LibreHardwareMonitorLib.dll` or `hwmon`), GPU core/hotspot/power/clocks (`nvidia-smi` / LHM), NVMe SSD temperature (`Get-StorageReliabilityCounter` / `smartctl`), and macOS `pmset` thermal throttle state.

---

## Quick Start (Windows 11 — AMD AM5 / Desktop PC)

### 1. Prerequisites
Open PowerShell and install `uv` and `git` if not already installed:
```powershell
winget install --id astral-sh.uv -e
winget install --id Git.Git -e
```
*(Optional for full Ryzen AM5 `Tctl/Tdie` CPU die temps and motherboard fan RPMs: install [FanControl](https://getfancontrol.com/) or place `LibreHardwareMonitorLib.dll` + `HidSharp.dll` in `.cache\tools\LibreHardwareMonitor\`. NVMe SSD and NVIDIA GPU temps work automatically without it.)*

### 2. Clone and Check Readiness
```powershell
git clone <YOUR_GITHUB_REPO_URL>
cd rig-bench
uv run bench.py status
```
Ensure the target drive has **>= 25 GiB free** for the 80M-row DuckDB + `fio` out-of-core tests.

### 3. Bootstrap Benchmark Tools and LLM Weights
```powershell
uv run bench.py bootstrap --with-geekbench --tier1-model
```
* **NVIDIA GPU (`llama-bench` CUDA)**: Download the latest `llama-*-bin-win-cuda-cu12.4-x64.zip` and `cudart-llama-bin-win-cu12.4-x64.zip` from `ggml-org/llama.cpp` GitHub Releases and extract into `.cache\tools\llama.cpp\`.
* **AMD Radeon GPU (`llama-bench` Vulkan)**: Download `llama-*-bin-win-vulkan-x64.zip` from `ggml-org/llama.cpp` GitHub Releases and extract into `.cache\tools\llama.cpp\`.
* **PyTorch CUDA (optional auto-install)**: `uv pip install torch --index-url https://download.pytorch.org/whl/cu124` (if you want CUDA acceleration in PyTorch GEMM/FFT).

### 4. Run a Quick Smoke Test (~2–3 min)
```powershell
uv run bench.py run --quick
```

### 5. Run the Full Suite and Open the Web UI
Tag each BIOS/tuning pass with `--profile-id` (for example, `am5-stock` before enabling EXPO/PBO, then `am5-pbo-expo` after):
```powershell
uv run bench.py run --profile-id stock --notes "Stock BIOS baseline"
uv run bench.py serve
```
Open **`http://localhost:8788`** in your browser.
* Runs are saved to `results/runs/<machine_id>/<timestamp>.json`.
* Only complete full-suite runs (`quick_mode == false` with all 4 suites and 22 core metrics) appear in the Web UI. Partial/aborted runs are safely parked in `results/incomplete/`.
* **Compare across friends/machines**: Drop another machine's run JSON into `results/runs/<their_machine_id>/`, run `uv run bench.py build-manifest`, and refresh the Web UI.

---

## Live Temperature and Fan RPM Dashboard (`rig-thermal-monitor/`)

For continuous **2-second live temperature, fan RPM, and CSV logging** on Windows 10/11 (with an interactive Web UI at `http://localhost:8095/`), see [`rig-thermal-monitor/README.md`](rig-thermal-monitor/README.md) and [`rig-thermal-monitor/SKILL.md`](rig-thermal-monitor/SKILL.md):

```powershell
cd rig-thermal-monitor
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_deps.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/probe.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/start_monitor.ps1
```

---

## Quick Start (macOS — Apple Silicon)
```bash
git clone <YOUR_GITHUB_REPO_URL>
cd rig-bench
uv run bench.py status
uv run bench.py bootstrap --with-geekbench --tier1-model
uv run bench.py run --profile-id m4-stock --notes "Stock baseline"
uv run bench.py serve
```
