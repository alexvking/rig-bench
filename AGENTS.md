# rig-bench Agent Instructions

Cross-platform CLI benchmark harness for CPU, GPU graphics/math, LLM throughput, out-of-core DuckDB/NVMe I/O, and continuous thermal/power telemetry on Windows 11 (AMD AM4/AM5, Intel, NVIDIA/AMD), macOS (Apple Silicon), and Linux.

## Mandatory Workflow for AI Agents
Whenever asked to set up, verify, or run `rig-bench` on this machine, read `.agent/skills/benchmark/SKILL.md` first and execute via `uv`:

1. **Preflight Hardware and Tooling**: `uv run bench.py status`
2. **Bootstrap Tools and Models**: `uv run bench.py bootstrap --with-geekbench --tier1-model`
3. **Verify Fast Smoke Pass**: `uv run bench.py run --quick`
4. **Execute Full Production Run**:
   `uv run bench.py run --profile-id <slug> --notes "<tuning description>"`
   *(Optional: pass `--price-usd <cost>` and `--idle-watts <watts>` for Performance-per-Dollar and Performance-per-Watt comparisons.)*
5. **View Dashboard**: `uv run bench.py serve` → `http://localhost:8788`

## Rules
* Never commit `.scratch/`, `.cache/`, `.venv/`, `rig-config.json`, or `results/incomplete/`.
* Only full 4-suite runs (`quick_mode == false`, all 22 core metrics valid) are admitted to `docs/data/manifest.json` and the Web UI.
