#!/usr/bin/env python3
from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from runners.common import (
    DOCS_DATA_DIR,
    DOCS_DIR,
    REPO_ROOT,
    RESULTS_DIR,
    SCRATCH_DIR,
    find_binary,
    get_external_cache_dir,
    get_uv_env,
    is_macos,
    is_windows,
    run_cmd,
)
from runners.cpu import run_cpu_suite
from runners.gpu_compute import MODEL_SPECS, run_gpu_compute_suite
from runners.gpu_raster import parse_3dmark_result_archive, run_gpu_raster_suite
from runners.storage_compute import run_storage_compute_suite
from runners.sysinfo import collect_sysinfo
from runners.thermals import ThermalMonitor

console = Console()

METRIC_CATALOG: list[dict[str, Any]] = [
    # CPU
    {"id": "sevenzip_1t_mips", "category": "CPU", "label": "7-Zip Single-Thread", "unit": "MIPS", "higher_is_better": True},
    {"id": "sevenzip_mt_mips", "category": "CPU", "label": "7-Zip Multi-Thread", "unit": "MIPS", "higher_is_better": True},
    {"id": "cpu_fp64_gemm_gflops", "category": "CPU", "label": "CPU Dense FP64 GEMM", "unit": "GFLOPS", "higher_is_better": True},
    {"id": "sha256_1t_mb_s", "category": "CPU", "label": "SHA-256 Single-Core", "unit": "MB/s", "higher_is_better": True},
    {"id": "sha256_mt_mb_s", "category": "CPU", "label": "SHA-256 All-Core", "unit": "MB/s", "higher_is_better": True},
    {"id": "geekbench6_single", "category": "CPU", "label": "Geekbench 6 Single-Core", "unit": "pts", "higher_is_better": True},
    {"id": "geekbench6_multi", "category": "CPU", "label": "Geekbench 6 Multi-Core", "unit": "pts", "higher_is_better": True},
    {"id": "cinebench24_single", "category": "CPU", "label": "Cinebench 2024 Single-Core", "unit": "pts", "higher_is_better": True},
    {"id": "cinebench24_multi", "category": "CPU", "label": "Cinebench 2024 Sustained Multi", "unit": "pts", "higher_is_better": True},
    # GPU Graphics / Raster
    {"id": "blender_eevee_1440p_fps", "category": "GPU Graphics", "label": "Blender EEVEE 1440p Raster", "unit": "FPS", "higher_is_better": True},
    {"id": "gravitymark_1440p_fps", "category": "GPU Graphics", "label": "GravityMark 1440p (200k)", "unit": "FPS", "higher_is_better": True},
    {"id": "gravitymark_1440p_score", "category": "GPU Graphics", "label": "GravityMark 1440p Score", "unit": "pts", "higher_is_better": True},
    {"id": "steel_nomad_light_score", "category": "GPU Graphics", "label": "3DMark Steel Nomad Light", "unit": "pts", "higher_is_better": True},
    # GPU Parallel Math
    {"id": "gpu_fp32_gemm_tflops", "category": "GPU Math", "label": "PyTorch FP32 GEMM (4096)", "unit": "TFLOPS", "higher_is_better": True},
    {"id": "gpu_fp16_gemm_tflops", "category": "GPU Math", "label": "PyTorch FP16 GEMM (8192)", "unit": "TFLOPS", "higher_is_better": True},
    {"id": "gpu_fft2d_gflops", "category": "GPU Math", "label": "PyTorch 2D Complex FFT", "unit": "GFLOPS", "higher_is_better": True},
    {"id": "gpu_mem_triad_gb_s", "category": "GPU Math", "label": "GPU Memory Triad Bandwidth", "unit": "GB/s", "higher_is_better": True},
    {"id": "cycles_gpu_spm", "category": "GPU Math", "label": "Blender Cycles GPU Path Trace", "unit": "samples/min", "higher_is_better": True},
    {"id": "llama_7b_pp512", "category": "GPU Math", "label": "LLM 7B Prompt Compute (pp512)", "unit": "tok/s", "higher_is_better": True},
    {"id": "llama_7b_tg128", "category": "GPU Math", "label": "LLM 7B Token Gen BW (tg128)", "unit": "tok/s", "higher_is_better": True},
    {"id": "llama_14b_pp512", "category": "GPU Math", "label": "LLM 14B (>6GB) Prompt (pp512)", "unit": "tok/s", "higher_is_better": True},
    {"id": "llama_14b_tg128", "category": "GPU Math", "label": "LLM 14B (>6GB) Token Gen (tg128)", "unit": "tok/s", "higher_is_better": True},
    # Storage + Out-of-Core Compute
    {"id": "fio_seq_read_mb_s", "category": "SSD and I/O Compute", "label": "fio Seq Read (1M QD8)", "unit": "MB/s", "higher_is_better": True},
    {"id": "fio_seq_write_mb_s", "category": "SSD and I/O Compute", "label": "fio Seq Write (1M QD8)", "unit": "MB/s", "higher_is_better": True},
    {"id": "fio_rand_read_qd1_mb_s", "category": "SSD and I/O Compute", "label": "fio 4K Rand Read (QD1)", "unit": "MB/s", "higher_is_better": True},
    {"id": "fio_rand_read_qd1_lat_us", "category": "SSD and I/O Compute", "label": "fio 4K QD1 Read Latency", "unit": "us", "higher_is_better": False},
    {"id": "fio_rand_read_qd32_iops", "category": "SSD and I/O Compute", "label": "fio 4K Rand Read (QD32)", "unit": "IOPS", "higher_is_better": True},
    {"id": "duckdb_ingest_mb_s", "category": "SSD and I/O Compute", "label": "DuckDB Parquet Ingest Write", "unit": "MB/s", "higher_is_better": True},
    {"id": "duckdb_spill_rows_m_s", "category": "SSD and I/O Compute", "label": "DuckDB Out-of-Core Spill Join", "unit": "M rows/s", "higher_is_better": True},
    {"id": "zstd_stream_mb_s", "category": "SSD and I/O Compute", "label": "zstd Parallel SSD Recompress", "unit": "MB/s", "higher_is_better": True},
    {"id": "pipeline_total_sec", "category": "SSD and I/O Compute", "label": "Out-of-Core Pipeline Total Time", "unit": "sec", "higher_is_better": False},
]


def extract_flat_metrics(suites: dict[str, Any]) -> dict[str, float]:
    flat: dict[str, float] = {}

    def _put(key: str, val: Any) -> None:
        if isinstance(val, (int, float)) and val is not None:
            flat[key] = round(float(val), 3)

    cpu = suites.get("cpu", {})
    sz = cpu.get("sevenzip", {})
    _put("sevenzip_1t_mips", sz.get("single_thread", {}).get("total_rating_mips"))
    _put("sevenzip_mt_mips", sz.get("multi_thread", {}).get("total_rating_mips"))
    cmc = cpu.get("cpu_math_crypto", {})
    _put("cpu_fp64_gemm_gflops", cmc.get("fp64_gemm_gflops"))
    _put("sha256_1t_mb_s", cmc.get("sha256_single_core_mb_s"))
    _put("sha256_mt_mb_s", cmc.get("sha256_multi_core_mb_s"))
    gb = cpu.get("geekbench6", {})
    _put("geekbench6_single", gb.get("single_core"))
    _put("geekbench6_multi", gb.get("multi_core"))
    cb = cpu.get("cinebench2024", {})
    _put("cinebench24_single", cb.get("single_core"))
    _put("cinebench24_multi", cb.get("multi_core"))

    gr = suites.get("gpu_raster", {})
    _put("blender_eevee_1440p_fps", gr.get("blender_eevee_1440p", {}).get("fps_1440p"))
    gm = gr.get("gravitymark_1440p", {})
    _put("gravitymark_1440p_fps", gm.get("avg_fps_1440p"))
    _put("gravitymark_1440p_score", gm.get("score_1440p"))
    tm = gr.get("threedmark", {})
    _put("steel_nomad_light_score", tm.get("steel_nomad_light_score"))

    gc = suites.get("gpu_compute", {})
    pt = gc.get("pytorch_parallel_math", {})
    _put("gpu_fp32_gemm_tflops", pt.get("fp32_gemm_4096_tflops"))
    _put("gpu_fp16_gemm_tflops", pt.get("fp16_gemm_8192_tflops"))
    _put("gpu_fft2d_gflops", pt.get("fft2d_4096_gflops"))
    _put("gpu_mem_triad_gb_s", pt.get("memory_triad_gb_s"))
    _put("cycles_gpu_spm", gc.get("blender_cycles_gpu", {}).get("samples_per_min"))
    lb_tiers = gc.get("llama_bench", {}).get("tiers", {})
    _put("llama_7b_pp512", lb_tiers.get("tier1_7b_q4km", {}).get("pp512_tok_s"))
    _put("llama_7b_tg128", lb_tiers.get("tier1_7b_q4km", {}).get("tg128_tok_s"))
    _put("llama_14b_pp512", lb_tiers.get("tier2_14b_q4km", {}).get("pp512_tok_s"))
    _put("llama_14b_tg128", lb_tiers.get("tier2_14b_q4km", {}).get("tg128_tok_s"))

    sc = suites.get("storage_compute", {})
    fio_jobs = sc.get("fio_nvme", {}).get("jobs", {})
    _put("fio_seq_read_mb_s", fio_jobs.get("seq_read_1m_qd8", {}).get("bw_mb_s"))
    _put("fio_seq_write_mb_s", fio_jobs.get("seq_write_1m_qd8", {}).get("bw_mb_s"))
    _put("fio_rand_read_qd1_mb_s", fio_jobs.get("rand_read_4k_qd1", {}).get("bw_mb_s"))
    _put("fio_rand_read_qd1_lat_us", fio_jobs.get("rand_read_4k_qd1", {}).get("mean_lat_us"))
    _put("fio_rand_read_qd32_iops", fio_jobs.get("rand_read_4k_qd32", {}).get("iops"))

    dk = sc.get("duckdb_zstd_out_of_core", {})
    _put("duckdb_ingest_mb_s", dk.get("phase1_ingest_mb_s"))
    _put("duckdb_spill_rows_m_s", dk.get("phase2_rows_per_sec_millions"))
    _put("zstd_stream_mb_s", dk.get("phase3_stream_mb_s"))
    _put("pipeline_total_sec", dk.get("total_pipeline_sec"))

    return flat


REQUIRED_SUITES: tuple[str, ...] = ("cpu", "gpu_raster", "gpu_compute", "storage_compute")

REQUIRED_FULL_RUN_METRICS: tuple[str, ...] = (
    "sevenzip_1t_mips",
    "sevenzip_mt_mips",
    "cpu_fp64_gemm_gflops",
    "sha256_1t_mb_s",
    "sha256_mt_mb_s",
    "blender_eevee_1440p_fps",
    "gpu_fp32_gemm_tflops",
    "gpu_fp16_gemm_tflops",
    "gpu_fft2d_gflops",
    "gpu_mem_triad_gb_s",
    "cycles_gpu_spm",
    "llama_7b_pp512",
    "llama_7b_tg128",
    "fio_seq_read_mb_s",
    "fio_seq_write_mb_s",
    "fio_rand_read_qd1_mb_s",
    "fio_rand_read_qd1_lat_us",
    "fio_rand_read_qd32_iops",
    "duckdb_ingest_mb_s",
    "duckdb_spill_rows_m_s",
    "zstd_stream_mb_s",
    "pipeline_total_sec",
)


def is_full_complete_run(doc: dict[str, Any]) -> bool:
    """Return True only if a run completed all 4 suites in full mode with all 22 core metrics."""
    if doc.get("quick_mode", False):
        return False
    if doc.get("ui_eligible") is False or doc.get("status") in {"incomplete", "partial", "crashed", "aborted"}:
        return False
    suites = doc.get("suites")
    if not isinstance(suites, dict) or not all(s in suites for s in REQUIRED_SUITES):
        return False
    flat = doc.get("flat_metrics") or extract_flat_metrics(suites)
    for key in REQUIRED_FULL_RUN_METRICS:
        val = flat.get(key)
        if not isinstance(val, (int, float)) or val <= 0:
            return False
    return True


def _classify_run_profile(run: dict[str, Any], base_sysinfo_by_mid: dict[str, dict[str, Any]]) -> tuple[str, str, dict[str, Any]]:
    """Return (profile_id, tuning_state, enriched_sysinfo) for a run document."""
    raw_sys = dict(run.get("sysinfo", {}))
    mid = raw_sys.get("machine_id", "unknown-rig")
    notes = (run.get("notes") or "").strip()
    notes_lower = notes.lower()

    explicit_pid = run.get("profile_id") or raw_sys.get("profile_id")
    is_souped = (
        (explicit_pid and "souped" in str(explicit_pid).lower())
        or "souped up" in notes_lower
    )
    is_oc = (
        is_souped
        or run.get("tuning_state") == "overclocked"
        or (explicit_pid and explicit_pid.endswith("-oc"))
        or "overclock" in notes_lower
        or "gentle oc" in notes_lower
    )

    if explicit_pid:
        profile_id = str(explicit_pid)
    elif is_oc:
        profile_id = f"{mid.removesuffix('-pc')}-oc" if mid.endswith("-pc") else f"{mid}-oc"
    elif mid == "zen2-1660s-pc":
        profile_id = "zen2-1660s-stock"
    else:
        profile_id = mid

    tuning_state = "souped_up" if is_souped else ("overclocked" if is_oc else (run.get("tuning_state") or "stock"))

    # Inherit baseline pricing / idle telemetry from earlier runs of the same machine_id when absent
    prior = base_sysinfo_by_mid.get(mid, {})
    for field in ("price_usd", "price_note", "idle_wall_watts"):
        if field not in raw_sys and field in prior:
            raw_sys[field] = prior[field]

    raw_sys["profile_id"] = profile_id
    raw_sys["hardware_machine_id"] = mid

    if is_souped and "zen2-1660s" in profile_id:
        if "Basement Gaming PC" in raw_sys.get("display_name", "") or "short_name" not in raw_sys:
            raw_sys["display_name"] = "Basement PC — Souped Up (CPU OC + DDR4-3600 + Cooling)"
            raw_sys["short_name"] = "Desktop (Souped Up)"
        raw_sys.setdefault("ram_tuning_note", "DDR4-3600 + OC CPU + Cooler/Exhaust/SSD Heatsink")
        if "workload_wall_watts" not in raw_sys:
            raw_sys["workload_wall_watts"] = {
                "CPU": 148.0,
                "GPU Graphics": 195.0,
                "GPU Math": 185.0,
                "SSD and I/O Compute": 125.0,
            }
            raw_sys["estimated_peak_watts"] = 195
    elif is_oc and "zen2-1660s" in profile_id:
        if "Basement Gaming PC" in raw_sys.get("display_name", "") or "short_name" not in raw_sys:
            raw_sys["display_name"] = "Basement PC — Gentle OC (After OC)"
            raw_sys["short_name"] = "Desktop (After OC)"
        if "workload_wall_watts" not in raw_sys:
            # 125W -> 150W GPU PL (+25W ceiling) + Zen 2 OC headroom
            raw_sys["workload_wall_watts"] = {
                "CPU": 148.0,
                "GPU Graphics": 195.0,
                "GPU Math": 185.0,
                "SSD and I/O Compute": 130.0,
            }
            raw_sys["estimated_peak_watts"] = 195
        if "gpu_power_telemetry_nvidia_smi" not in raw_sys:
            raw_sys["gpu_power_telemetry_nvidia_smi"] = {
                "default_tdp_limit_watts": 125.0,
                "active_power_limit_watts": 150.0,
                "max_boost_clock_mhz": 2130,
                "mem_clock_mhz": 7850,
            }
    elif profile_id == "zen2-1660s-stock":
        raw_sys.setdefault("short_name", "Desktop (Before OC)")
    elif profile_id == "m4-mac-mini":
        raw_sys.setdefault("short_name", "M4 Mac Mini")
    else:
        raw_sys.setdefault("short_name", raw_sys.get("display_name") or profile_id)

    return profile_id, tuning_state, raw_sys


def build_manifest() -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)

    runs: list[dict[str, Any]] = []
    for json_file in sorted(RESULTS_DIR.rglob("*.json")):
        try:
            doc = json.loads(json_file.read_text(encoding="utf-8"))
            if "sysinfo" in doc and "suites" in doc:
                doc["flat_metrics"] = extract_flat_metrics(doc["suites"])
                if not is_full_complete_run(doc):
                    continue
                doc["rel_path"] = str(json_file.relative_to(REPO_ROOT).as_posix())
                runs.append(doc)
        except Exception as exc:
            console.print(f"[yellow]Skipping malformed run file {json_file}: {exc}[/yellow]")

    sorted_runs = sorted(runs, key=lambda r: r.get("timestamp_utc", ""))

    base_sysinfo_by_mid: dict[str, dict[str, Any]] = {}
    for run in sorted_runs:
        mid = run["sysinfo"].get("machine_id")
        if mid and ("price_usd" in run["sysinfo"] or mid not in base_sysinfo_by_mid):
            base_sysinfo_by_mid[mid] = dict(run["sysinfo"])

    # Group latest merged view per machine_id AND per distinct configuration profile_id
    machines: dict[str, dict[str, Any]] = {}
    profiles_unsorted: dict[str, dict[str, Any]] = {}

    for run in sorted_runs:
        mid = run["sysinfo"]["machine_id"]
        is_quick = bool(run.get("quick_mode", False))
        profile_id, tuning_state, enriched_sysinfo = _classify_run_profile(run, base_sysinfo_by_mid)
        run["profile_id"] = profile_id
        run["tuning_state"] = tuning_state

        # 1. Hardware machine_id rollup (legacy/latest per physical box)
        m_entry = machines.setdefault(
            mid,
            {
                "machine_id": mid,
                "sysinfo": enriched_sysinfo,
                "latest_timestamp_utc": run.get("timestamp_utc"),
                "latest_quick_mode": is_quick,
                "merged_metrics": {},
                "merged_suites": {},
                "run_count": 0,
            },
        )
        m_entry["sysinfo"] = enriched_sysinfo
        m_entry["latest_timestamp_utc"] = run.get("timestamp_utc")
        m_entry["latest_quick_mode"] = is_quick
        m_entry["run_count"] += 1
        for k, v in run.get("flat_metrics", {}).items():
            if not is_quick or k not in m_entry["merged_metrics"] or m_entry.get("_metric_was_quick", {}).get(k, True):
                m_entry["merged_metrics"][k] = v
                m_entry.setdefault("_metric_was_quick", {})[k] = is_quick
        for sk, sv in run.get("suites", {}).items():
            m_entry["merged_suites"][sk] = sv

        # 2. Configuration profile_id rollup (supports Stock vs Overclocked side-by-side)
        p_entry = profiles_unsorted.setdefault(
            profile_id,
            {
                "profile_id": profile_id,
                "machine_id": profile_id,
                "hardware_machine_id": mid,
                "tuning_state": tuning_state,
                "notes": run.get("notes") or "",
                "sysinfo": enriched_sysinfo,
                "latest_timestamp_utc": run.get("timestamp_utc"),
                "latest_quick_mode": is_quick,
                "merged_metrics": {},
                "merged_suites": {},
                "run_count": 0,
            },
        )
        p_entry["tuning_state"] = tuning_state
        if run.get("notes"):
            p_entry["notes"] = run["notes"]
        if run.get("thermals"):
            p_entry["latest_thermals"] = run["thermals"]
        p_entry["sysinfo"] = enriched_sysinfo
        p_entry["latest_timestamp_utc"] = run.get("timestamp_utc")
        p_entry["latest_quick_mode"] = is_quick
        p_entry["run_count"] += 1
        for k, v in run.get("flat_metrics", {}).items():
            if not is_quick or k not in p_entry["merged_metrics"] or p_entry.get("_metric_was_quick", {}).get(k, True):
                p_entry["merged_metrics"][k] = v
                p_entry.setdefault("_metric_was_quick", {})[k] = is_quick
        for sk, sv in run.get("suites", {}).items():
            p_entry["merged_suites"][sk] = sv

    for m in machines.values():
        m.pop("_metric_was_quick", None)
    for p in profiles_unsorted.values():
        p.pop("_metric_was_quick", None)

    preferred_order = ["m4-mac-mini", "zen2-1660s-stock", "zen2-1660s-oc", "zen2-1660s-souped-up"]
    ordered_keys = [k for k in preferred_order if k in profiles_unsorted] + [
        k for k in sorted(profiles_unsorted.keys()) if k not in preferred_order
    ]
    profiles = {k: profiles_unsorted[k] for k in ordered_keys}

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "metric_catalog": METRIC_CATALOG,
        "profiles": profiles,
        "machines": machines,
        "runs": runs,
    }
    manifest_path = DOCS_DATA_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def cmd_status(_args: argparse.Namespace) -> None:
    info = collect_sysinfo()
    ext_cache = get_external_cache_dir()

    console.print(f"\n[bold cyan]System Profile — {info['display_name']}[/bold cyan]")
    sys_table = Table(show_header=False, box=None, pad_edge=False)
    sys_table.add_row("Machine ID", f"[bold]{info['machine_id']}[/bold]")
    sys_table.add_row("OS / Arch", f"{info['os']} {info['os_release']} ({info['arch']})")
    sys_table.add_row("CPU", f"{info['cpu_model']} ({info['cpu_logical_cores']} threads)")
    sys_table.add_row("GPU", f"{info['gpu_model']} [{info['gpu_api_primary']}]")
    sys_table.add_row("Memory", f"{info['ram_total_gb']} GiB ({info['gpu_memory_type']})")
    sys_table.add_row(
        "Target SSD",
        f"{info['ssd_model']} — [bold]{info['ssd_free_gb_at_start']} GiB free[/bold] / {info['ssd_total_gb']} GiB ({info['ssd_used_percent_at_start']}% used)",
    )
    sys_table.add_row("External Cache", str(ext_cache))
    console.print(sys_table)

    checks = [
        ("7-Zip CLI", find_binary(["7zz", "7z"], ["/opt/homebrew/bin/7zz"], [r"C:\Program Files\7-Zip\7z.exe"]), "Core CPU"),
        ("fio", find_binary(["fio"], ["/opt/homebrew/bin/fio"], [r"C:\Program Files\fio\fio.exe"]), "Raw NVMe I/O"),
        ("Blender CLI", find_binary(["blender", "Blender"], ["/Applications/Blender.app/Contents/MacOS/Blender"], [r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe", r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"]), "GPU EEVEE + Cycles"),
        ("llama-bench", find_binary(["llama-bench"], ["/opt/homebrew/bin/llama-bench"]), "GPU LLM Compute/BW"),
        ("Geekbench 6", find_binary(["geekbench6"], ["/Applications/Geekbench 6.app/Contents/Resources/geekbench6"]), "CPU Burst IPC"),
        ("GravityMark", find_binary(["GravityMark"], ["/Applications/GravityMark.app/Contents/MacOS/GravityMark"], [r"C:\Program Files\GravityMark\bin\GravityMark.exe"]), "Optional 1440p 200k GPU"),
        ("Cinebench 2024", find_binary(["Cinebench"], ["/Applications/Cinebench.app/Contents/MacOS/Cinebench"], [r"C:\Cinebench2024\Cinebench.exe"]), "Optional 10m CPU Thermal"),
    ]

    console.print("\n[bold cyan]CLI Tool Readiness[/bold cyan]")
    t = Table("Tool", "Role", "Status / Path")
    for label, path, role in checks:
        status_str = f"[green]Ready[/green] ({path})" if path else "[yellow]Not installed[/yellow]"
        t.add_row(label, role, status_str)
    console.print(t)


def _download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    console.print(f"[cyan]Downloading {dest.name} -> {dest}...[/cyan]")
    req = urllib.request.Request(url, headers={"User-Agent": "rig-bench/0.1"})
    with urllib.request.urlopen(req) as resp, tmp.open("wb") as out:
        total = int(resp.headers.get("Content-Length", "0") or "0")
        downloaded = 0
        last_pct = -5
        while True:
            chunk = resp.read(8 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = int(downloaded * 100 / total)
                if pct >= last_pct + 10:
                    last_pct = pct
                    console.print(f"  {pct}% ({round(downloaded / (1024**3), 2)} / {round(total / (1024**3), 2)} GiB)")
    tmp.replace(dest)


def cmd_bootstrap(args: argparse.Namespace) -> None:
    if is_macos():
        console.print("[bold cyan]Bootstrapping macOS CLI benchmark tools via Homebrew...[/bold cyan]")
        env = get_uv_env()
        env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
        formulas = [f for f in ("sevenzip", "fio", "llama.cpp") if not shutil.which("7zz" if f == "sevenzip" else ("llama-bench" if f == "llama.cpp" else f))]
        if formulas:
            subprocess.run(["brew", "install", *formulas], input="y\ny\n", text=True, check=False, env=env)
        if args.with_geekbench:
            gb_app = Path.home() / "Applications" / "Geekbench 6.app"
            if not gb_app.exists() and not Path("/Applications/Geekbench 6.app").exists():
                gb_app.parent.mkdir(parents=True, exist_ok=True)
                zip_dest = get_external_cache_dir() / "Geekbench-6.4.0-Mac.zip"
                if not zip_dest.is_file():
                    _download_file("https://cdn.geekbench.com/Geekbench-6.4.0-Mac.zip", zip_dest)
                subprocess.run(["ditto", "-x", "-k", str(zip_dest), str(gb_app.parent)], check=True)
                console.print(f"[green]Installed pinned Geekbench 6.4.0 -> {gb_app}[/green]")
    elif is_windows():
        console.print("[bold cyan]Bootstrapping Windows 11 CLI benchmark tools via winget...[/bold cyan]")
        packages = [
            "7zip.7zip",
            "fio.fio",
            "BlenderFoundation.Blender",
        ]
        for pkg in packages:
            subprocess.run(
                ["winget", "install", "--id", pkg, "-e", "--accept-source-agreements", "--accept-package-agreements"],
                check=False,
            )
        if args.with_geekbench:
            gb_exe = Path(r"C:\Program Files\Geekbench 6\geekbench6.exe")
            gb_exe86 = Path(r"C:\Program Files (x86)\Geekbench 6\geekbench6.exe")
            if not gb_exe.exists() and not gb_exe86.exists():
                setup_exe = get_external_cache_dir() / "Geekbench-6.4.0-WindowsSetup.exe"
                if not setup_exe.is_file():
                    _download_file("https://cdn.geekbench.com/Geekbench-6.4.0-WindowsSetup.exe", setup_exe)
                subprocess.run([str(setup_exe), "/S"], check=False)
                console.print("[green]Installed pinned Geekbench 6.4.0 on Windows[/green]")
        console.print(
            "[dim]Tip: For llama-bench on Windows CUDA 12, extract the official llama.cpp win-cuda-cu12.4-x64 release zip into .cache/tools/llama.cpp/[/dim]"
        )

    if args.models or args.tier1_model:
        models_dir = get_external_cache_dir() / "models"
        specs = MODEL_SPECS[:1] if args.tier1_model and not args.models else MODEL_SPECS
        for spec in specs:
            dest = models_dir / spec["filename"]
            if dest.is_file() and dest.stat().st_size > 100_000_000:
                console.print(f"[green]Model already cached:[/green] {dest}")
            else:
                _download_file(spec["hf_urls"][0], dest)


def cmd_run(args: argparse.Namespace) -> None:
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    sysinfo = collect_sysinfo(
        machine_id=args.machine_id,
        scratch_target=SCRATCH_DIR,
        price_usd=getattr(args, "price_usd", None),
        idle_watts=getattr(args, "idle_watts", None),
    )
    machine_id = sysinfo["machine_id"]

    requested = [s.strip() for s in args.suites.split(",") if s.strip()]
    console.print(
        f"\n[bold green]Starting rig-bench on {sysinfo['display_name']}[/bold green] "
        f"(suites={requested}, quick={args.quick})"
    )

    suites_out: dict[str, Any] = {}
    t_start = datetime.now(timezone.utc)
    incomplete_dir = REPO_ROOT / "results" / "incomplete" / machine_id
    incomplete_dir.mkdir(parents=True, exist_ok=True)
    partial_file = incomplete_dir / f"{t_start.strftime('%Y%m%d_%H%M%S')}.partial.json"

    def _save_partial() -> None:
        partial_doc: dict[str, Any] = {
            "schema_version": "1.0",
            "status": "incomplete",
            "ui_eligible": False,
            "started_at_utc": t_start.isoformat(),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "quick_mode": bool(args.quick),
            "notes": args.notes or "",
            "sysinfo": sysinfo,
            "suites": suites_out,
        }
        if args.profile_id:
            partial_doc["profile_id"] = args.profile_id
        partial_doc["flat_metrics"] = extract_flat_metrics(suites_out)
        partial_file.write_text(json.dumps(partial_doc, indent=2), encoding="utf-8")

    monitor = ThermalMonitor(interval_sec=4.0)
    monitor.start()
    try:
        if "cpu" in requested:
            console.print("[cyan]-> Running CPU suite...[/cyan]")
            monitor.set_suite("cpu")
            suites_out["cpu"] = run_cpu_suite(quick=args.quick, skip_external_slow=args.skip_optional_gui_clis)
            _save_partial()

        if "gpu_raster" in requested:
            console.print("[cyan]-> Running GPU 3D Graphics / Raster suite...[/cyan]")
            monitor.set_suite("gpu_raster")
            suites_out["gpu_raster"] = run_gpu_raster_suite(quick=args.quick)
            _save_partial()

        if "gpu_compute" in requested:
            console.print("[cyan]-> Running GPU Parallel Math suite...[/cyan]")
            monitor.set_suite("gpu_compute")
            suites_out["gpu_compute"] = run_gpu_compute_suite(quick=args.quick)
            _save_partial()

        if "storage_compute" in requested:
            console.print("[cyan]-> Running SSD + Out-of-Core Compute suite...[/cyan]")
            monitor.set_suite("storage_compute")
            suites_out["storage_compute"] = run_storage_compute_suite(quick=args.quick)
            _save_partial()
    finally:
        thermals_summary = monitor.stop()

    t_end = datetime.now(timezone.utc)
    ts_slug = t_end.strftime("%Y%m%d_%H%M%S")
    out_dir = RESULTS_DIR / machine_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{ts_slug}.json"

    doc: dict[str, Any] = {
        "schema_version": "1.0",
        "timestamp_utc": t_end.isoformat(),
        "duration_sec": round((t_end - t_start).total_seconds(), 2),
        "quick_mode": bool(args.quick),
        "notes": args.notes or "",
        "sysinfo": sysinfo,
        "thermals": thermals_summary,
        "suites": suites_out,
    }
    if args.profile_id:
        doc["profile_id"] = args.profile_id
    doc["flat_metrics"] = extract_flat_metrics(suites_out)

    out_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    if is_full_complete_run(doc) and partial_file.exists():
        partial_file.unlink(missing_ok=True)
    manifest_path = build_manifest()

    console.print(f"\n[bold green]Saved run JSON:[/bold green] {out_file}")
    console.print(f"[bold green]Updated manifest:[/bold green] {manifest_path}")

    summary = Table("Metric", "Value", "Unit", title=f"Run Summary ({machine_id})")
    cat_by_id = {m["id"]: m for m in METRIC_CATALOG}
    for mid, val in doc["flat_metrics"].items():
        meta = cat_by_id.get(mid, {"label": mid, "unit": ""})
        summary.add_row(meta["label"], f"{val:,.2f}" if isinstance(val, float) else str(val), meta["unit"])
    console.print(summary)

    th_overall = thermals_summary.get("overall", {})
    if th_overall:
        th_table = Table("Sensor", "Min", "Avg", "Max", "End", title="Thermal Telemetry (During Run)")
        for key, label, unit in (
            ("cpu_temp_c", "CPU Temp", "C"),
            ("gpu_temp_c", "GPU Core", "C"),
            ("gpu_hotspot_c", "GPU Hot Spot", "C"),
            ("ssd_temp_c", "NVMe / SSD", "C"),
            ("gpu_power_w", "GPU Board Power", "W"),
            ("cpu_fan_rpm", "CPU Fan", "RPM"),
        ):
            s = th_overall.get(key)
            if isinstance(s, dict):
                th_table.add_row(label, f"{s['min']} {unit}", f"{s['avg']} {unit}", f"{s['max']} {unit}", f"{s['end']} {unit}")
        console.print(th_table)

    if args.commit_and_push:
        _git_sync_results(out_file, manifest_path, machine_id, push=True)
    elif args.commit:
        _git_sync_results(out_file, manifest_path, machine_id, push=False)


def _git_sync_results(run_file: Path, manifest_path: Path, machine_id: str, push: bool) -> None:
    rel_run = run_file.relative_to(REPO_ROOT).as_posix()
    rel_manifest = manifest_path.relative_to(REPO_ROOT).as_posix()
    if push:
        run_cmd(["git", "pull", "--rebase", "--autostash"], cwd=REPO_ROOT, timeout=60)
        # Rebuild manifest after pull so both machines' runs are merged cleanly
        build_manifest()
    run_cmd(["git", "add", rel_run, rel_manifest], cwd=REPO_ROOT, timeout=30)
    commit_proc = run_cmd(
        ["git", "commit", "-m", f"bench({machine_id}): add benchmark run {run_file.stem}"],
        cwd=REPO_ROOT,
        timeout=30,
    )
    console.print(f"[cyan]Git commit:[/cyan] {(commit_proc.stdout or commit_proc.stderr).strip()}")
    if push:
        push_proc = run_cmd(["git", "push"], cwd=REPO_ROOT, timeout=60)
        console.print(f"[cyan]Git push:[/cyan] {(push_proc.stdout or push_proc.stderr).strip()}")


def cmd_import_3dmark(args: argparse.Namespace) -> None:
    archive = Path(args.archive).expanduser().resolve()
    parsed = parse_3dmark_result_archive(archive)
    sysinfo = collect_sysinfo(machine_id=args.machine_id)
    mid = sysinfo["machine_id"]
    ts_slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = RESULTS_DIR / mid
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{ts_slug}_3dmark.json"

    score = args.score or parsed.get("steelnomadlightscore") or parsed.get("graphicsscore") or parsed.get("score")
    doc = {
        "schema_version": "1.0",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "duration_sec": 0.0,
        "quick_mode": False,
        "notes": f"Imported 3DMark archive {archive.name}",
        "sysinfo": sysinfo,
        "suites": {
            "gpu_raster": {
                "threedmark": {
                    "status": "ok",
                    "steel_nomad_light_score": float(score) if score is not None else None,
                    "raw_archive_fields": parsed,
                }
            }
        },
    }
    doc["flat_metrics"] = extract_flat_metrics(doc["suites"])
    out_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    build_manifest()
    console.print(f"[green]Imported 3DMark result -> {out_file}[/green]")


class LiveManifestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/data/manifest.json":
            try:
                build_manifest()
            except Exception:
                pass
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()


def _detect_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


class ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


def cmd_serve(args: argparse.Namespace) -> None:
    build_manifest()
    lan_ip = _detect_lan_ip()
    git_proc: subprocess.Popen[str] | None = None

    handler = functools.partial(LiveManifestHandler, directory=str(DOCS_DIR))
    server = ReusableThreadingHTTPServer((args.host, args.port), handler)

    if not args.no_git_daemon and is_macos():
        exec_proc = run_cmd(["git", "--exec-path"], timeout=5)
        git_daemon_bin = Path(exec_proc.stdout.strip()) / "git-daemon"
        if git_daemon_bin.is_file():
            base_dir = REPO_ROOT.parent
            git_cmd = [
                str(git_daemon_bin),
                "--reuseaddr",
                f"--base-path={base_dir}",
                "--export-all",
                "--enable=receive-pack",
                f"--listen={args.host}",
                f"--port={args.git_port}",
                str(REPO_ROOT),
            ]
            git_proc = subprocess.Popen(git_cmd, text=True)
            console.print(
                f"[bold green]Local Git Daemon listening:[/bold green] "
                f"git://{lan_ip}:{args.git_port}/{REPO_ROOT.name}"
            )

    console.print(
        f"[bold cyan]Interactive Benchmark Dashboard live at:[/bold cyan]\n"
        f"  Local: http://localhost:{args.port}\n"
        f"  LAN:   http://{lan_ip}:{args.port}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down server...[/yellow]")
    finally:
        server.server_close()
        if git_proc:
            git_proc.terminate()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="rig-bench: Cross-platform CLI benchmark harness and dashboard")
    sub = p.add_subparsers(dest="command", required=True)

    s_status = sub.add_parser("status", help="Inspect hardware profile, SSD headroom, and CLI binary readiness")
    s_status.set_defaults(func=cmd_status)

    s_boot = sub.add_parser("bootstrap", help="Install missing CLI benchmark tools and optional GGUF weights")
    s_boot.add_argument("--with-geekbench", action="store_true", help="Also install Geekbench 6 cask/winget package")
    s_boot.add_argument("--tier1-model", action="store_true", help="Download Tier 1 (~4.7 GB 7B Q4_K_M) GGUF model")
    s_boot.add_argument("--models", action="store_true", help="Download both Tier 1 (7B) and Tier 2 (14B) GGUF models")
    s_boot.set_defaults(func=cmd_bootstrap)

    s_run = sub.add_parser("run", help="Run cross-platform benchmark suites and save JSON telemetry")
    s_run.add_argument(
        "--suites",
        default="cpu,gpu_raster,gpu_compute,storage_compute",
        help="Comma-separated suites: cpu,gpu_raster,gpu_compute,storage_compute",
    )
    s_run.add_argument("--quick", action="store_true", help="Fast smoke/calibration run (~2-3 min total)")
    s_run.add_argument("--skip-optional-gui-clis", action="store_true", help="Skip Geekbench/Cinebench if only testing built-in CLIs")
    s_run.add_argument("--machine-id", default=None, help="Override machine_id slug (e.g. am5-9800x3d-pc, m4-mac-mini)")
    s_run.add_argument("--profile-id", default=None, help="Optional config profile slug (e.g. stock, pbo-expo, oc)")
    s_run.add_argument("--price-usd", type=int, default=None, help="Optional system cost in USD for Perf-per-Dollar lens")
    s_run.add_argument("--idle-watts", type=float, default=None, help="Optional measured idle wall watts")
    s_run.add_argument("--notes", default="", help="Optional run annotation")
    s_run.add_argument("--commit", action="store_true", help="Commit new run JSON + manifest locally")
    s_run.add_argument("--commit-and-push", action="store_true", help="Pull --rebase, commit, and push to origin")
    s_run.set_defaults(func=cmd_run)

    s_3d = sub.add_parser("import-3dmark", help="Import a Steam .3dmark-result archive or manual score")
    s_3d.add_argument("archive", help="Path to .3dmark-result file")
    s_3d.add_argument("--score", type=float, default=None, help="Optional explicit score override")
    s_3d.add_argument("--machine-id", default=None, help="Override machine_id slug")
    s_3d.set_defaults(func=cmd_import_3dmark)

    s_man = sub.add_parser("build-manifest", help="Rebuild docs/data/manifest.json from results/runs/")
    s_man.set_defaults(func=lambda _a: console.print(f"Built {build_manifest()}"))

    s_srv = sub.add_parser("serve", help="Serve interactive Web UI (:8788) and local LAN Git daemon (:9418)")
    s_srv.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    s_srv.add_argument("--port", type=int, default=8788, help="Web UI HTTP port (default 8788)")
    s_srv.add_argument("--git-port", type=int, default=9418, help="Local Git daemon port (default 9418)")
    s_srv.add_argument("--no-git-daemon", action="store_true", help="Serve HTTP dashboard only")
    s_srv.set_defaults(func=cmd_serve)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
