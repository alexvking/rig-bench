---
name: benchmark
description: Bootstrap CLI dependencies, verify thermal sensors, run cross-platform CPU/GPU/SSD benchmarks on Windows 11 (including AMD AM5), macOS, or Linux, and view results in the Web UI.
---

# Cross-Platform Benchmark Execution Skill

Follow these steps to set up and run `rig-bench` on **Windows 11 (AMD AM5 / Desktop PC)**, **macOS (Apple Silicon)**, or **Linux**.

## Step 1 — Preflight Check
Ensure `uv` and `git` are installed:
* **Windows 11 (PowerShell)**: `winget install --id astral-sh.uv -e; winget install --id Git.Git -e`
* **macOS**: `which uv git`

Inspect hardware detection, RAM speed (`DDR5-6000` / `DDR4-3600`), free SSD headroom (must have **>= 25 GiB free** for the 80M-row DuckDB out-of-core spill test), and CLI binary paths:
```bash
uv run bench.py status
```

## Step 2 — Bootstrap CLI Binaries and Models
Install missing CLI tools (`7-Zip`, `fio`, `Blender`, `Geekbench 6`) and download the deterministic Tier 1 LLM weight (`Qwen2.5-7B Q4_K_M`, ~4.7 GB):
```bash
uv run bench.py bootstrap --with-geekbench --tier1-model
```

* **Windows 11 `llama-bench` GPU acceleration**:
  * **NVIDIA RTX/GTX**: Download `llama-*-bin-win-cuda-cu12.4-x64.zip` + `cudart-llama-bin-win-cu12.4-x64.zip` from `ggml-org/llama.cpp` GitHub Releases and extract into `.cache\tools\llama.cpp\`.
  * **AMD Radeon / iGPU**: Download `llama-*-bin-win-vulkan-x64.zip` from `ggml-org/llama.cpp` GitHub Releases and extract into `.cache\tools\llama.cpp\`.
* **Windows 11 AMD AM5 CPU/Motherboard Thermals**:
  * `nvidia-smi` (GPU temp/power/clocks) and `Get-StorageReliabilityCounter` (NVMe temp) work out of the box.
  * For full Ryzen AM5 `Tctl/Tdie` die temperatures and fan RPMs, install **FanControl** or place `LibreHardwareMonitorLib.dll` + `HidSharp.dll` into `.cache\tools\LibreHardwareMonitor\` (or set `RIG_BENCH_LHM_DLL`).
* **PyTorch GPU**: If PyTorch defaults to CPU on Windows NVIDIA, install CUDA wheels inside the venv:
  ```powershell
  uv pip install torch --index-url https://download.pytorch.org/whl/cu124
  ```

## Step 3 — Smoke Test, Then Full Run
1. **Quick smoke test (~2–3 minutes)**:
   ```bash
   uv run bench.py run --quick
   ```
2. **Full benchmark run (~10–14 minutes)**:
   ```bash
   uv run bench.py run --profile-id stock --notes "Stock BIOS + EXPO off"
   ```
   After tuning (e.g., enabling DDR5 EXPO + PBO / Curve Optimizer on AM5), run again with a new `--profile-id` so both profiles appear side-by-side in the Web UI:
   ```bash
   uv run bench.py run --profile-id pbo-expo --notes "AM5 PBO + Curve Optimizer + DDR5-6000 EXPO"
   ```

## Step 4 — Launch the Interactive Web UI
```bash
uv run bench.py serve --no-git-daemon
```
Open `http://localhost:8788` to inspect the Butterfly comparison, 6-Axis Architecture Radar, Domain breakdowns, Thermals, Performance-per-Watt, Performance-per-Dollar, and full matrix.
