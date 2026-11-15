from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import numpy as np

from .common import find_binary, get_uv_env, is_macos, is_windows, run_cmd


def _sha256_worker(nbytes: int) -> float:
    buf = b"rig-bench-deterministic-block-0123456789abcdef" * 1400  # ~64 KB
    iters = max(1, nbytes // len(buf))
    h = hashlib.sha256()
    t0 = time.perf_counter()
    for _ in range(iters):
        h.update(buf)
    _ = h.digest()
    return iters * len(buf) / max(time.perf_counter() - t0, 1e-9)


def run_7zip_cli(quick: bool = False) -> dict[str, Any]:
    seven_z = find_binary(
        ["7zz", "7z"],
        mac_candidates=["/opt/homebrew/bin/7zz", "/usr/local/bin/7zz"],
        win_candidates=[r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"],
    )
    if not seven_z:
        hint = "brew install sevenzip" if is_macos() else "winget install 7zip.7zip"
        return {"status": "skipped", "reason": f"7-Zip CLI not found ({hint})"}

    passes = "1" if quick else "2"
    results: dict[str, Any] = {"status": "ok", "binary": seven_z}

    for mode, flag in (("single_thread", ["-mmt1"]), ("multi_thread", [])):
        proc = run_cmd([seven_z, "b", passes, "-md24", *flag], timeout=300)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        avr_match = re.search(
            r"Avr:\s+\d+\s+\d+\s+(\d+)\s+(\d+)\s+\|\s+\d+\s+\d+\s+(\d+)\s+(\d+)",
            out,
        )
        tot_match = re.search(r"Tot:\s+\d+\s+(\d+)\s+(\d+)", out)
        freq_match = re.findall(r"1T CPU Freq \(MHz\):([^\n]+)", out)
        peak_mhz = None
        if freq_match:
            nums = [int(x) for x in re.findall(r"\d+", freq_match[0])]
            peak_mhz = max(nums) if nums else None

        results[mode] = {
            "compress_mips": int(avr_match.group(2)) if avr_match else None,
            "decompress_mips": int(avr_match.group(4)) if avr_match else None,
            "total_rating_mips": int(tot_match.group(2)) if tot_match else None,
            "peak_1t_mhz": peak_mhz,
        }

    return results


def run_cpu_math_and_crypto(quick: bool = False) -> dict[str, Any]:
    """Deterministic cross-platform CPU FP64 GEMM (Accelerate/AMX vs OpenBLAS/AVX2) + SHA-256 throughput."""
    n = 2048 if quick else 3072
    rng = np.random.default_rng(42)
    a = rng.standard_normal((n, n), dtype=np.float64)
    b = rng.standard_normal((n, n), dtype=np.float64)

    # Warmup
    _ = a @ b
    iters = 3 if quick else 6
    t0 = time.perf_counter()
    for _ in range(iters):
        _ = a @ b
    elapsed = time.perf_counter() - t0
    flops_per_gemm = 2.0 * (n**3)
    cpu_fp64_gflops = (flops_per_gemm * iters / elapsed) / 1e9

    # Single-core vs All-core SHA-256
    bytes_per_worker = (256 if quick else 512) * 1024 * 1024
    st_bps = _sha256_worker(bytes_per_worker)

    workers = os.cpu_count() or 4
    t1 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_sha256_worker, [bytes_per_worker] * workers))
    mt_elapsed = time.perf_counter() - t1
    mt_bps = (bytes_per_worker * workers) / max(mt_elapsed, 1e-9)

    return {
        "status": "ok",
        "matrix_size": n,
        "fp64_gemm_gflops": round(cpu_fp64_gflops, 2),
        "sha256_single_core_mb_s": round(st_bps / (1024 * 1024), 1),
        "sha256_multi_core_mb_s": round(mt_bps / (1024 * 1024), 1),
        "sha256_workers": workers,
    }


def run_geekbench6_cli() -> dict[str, Any]:
    gb_bin = find_binary(
        ["geekbench6", "geekbench_aarch64", "geekbench_x86_64"],
        mac_candidates=[
            "~/Applications/Geekbench 6.app/Contents/Resources/geekbench6",
            "/Applications/Geekbench 6.app/Contents/Resources/geekbench6",
            "/opt/homebrew/bin/geekbench6",
        ],
        win_candidates=[
            r"C:\Program Files (x86)\Geekbench 6\geekbench6.exe",
            r"C:\Program Files\Geekbench 6\geekbench6.exe",
        ],
    )
    if not gb_bin:
        hint = "brew install --cask geekbench" if is_macos() else "winget install PrimateLabs.Geekbench.6"
        return {"status": "skipped", "reason": f"Geekbench 6 CLI not found ({hint})"}

    proc = run_cmd([gb_bin, "--cpu"], timeout=900)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    url_match = re.search(r"(https://browser\.geekbench\.com/v6/cpu/\d+)", out)
    single_score = None
    multi_score = None

    if url_match:
        result_url = url_match.group(1)
        # Note: browser.geekbench.com is gated by Cloudflare Turnstile/Challenge; preserve URL without scraping
        return {
            "status": "completed_url_only",
            "single_core": None,
            "multi_core": None,
            "result_url": result_url,
        }

    return {"status": "error", "reason": "No Geekbench result URL found", "tail": out[-400:]}


def run_cinebench_cli(quick: bool = False) -> dict[str, Any]:
    cb_bin = find_binary(
        ["Cinebench"],
        mac_candidates=[
            "/Applications/Cinebench.app/Contents/MacOS/Cinebench",
            "/Applications/Maxon Cinebench/Cinebench.app/Contents/MacOS/Cinebench",
        ],
        win_candidates=[
            r"C:\Cinebench2024\Cinebench.exe",
            r"C:\Program Files\Maxon Cinebench\Cinebench.exe",
        ],
    )
    if not cb_bin:
        return {
            "status": "skipped",
            "reason": "Cinebench 2024 not installed (optional sustained 10-min thermal test)",
        }

    min_dur = "60" if quick else "600"
    results: dict[str, Any] = {"status": "ok", "min_duration_sec": int(min_dur)}

    for label, flag in (("single_core", "g_CinebenchCpu1Test=true"), ("multi_core", "g_CinebenchCpuXTest=true")):
        proc = run_cmd([cb_bin, flag, f"g_CinebenchMinimumTestDuration={min_dur}"], timeout=1200, env=get_uv_env())
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        cb_matches = re.findall(r"CB\s+([0-9]+(?:\.[0-9]+)?)", out)
        results[label] = float(cb_matches[-1]) if cb_matches else None

    if results.get("single_core") is None and results.get("multi_core") is None:
        results["status"] = "completed_unparsed"
    return results


def run_cpu_suite(quick: bool = False, skip_external_slow: bool = False) -> dict[str, Any]:
    suite: dict[str, Any] = {
        "sevenzip": run_7zip_cli(quick=quick),
        "cpu_math_crypto": run_cpu_math_and_crypto(quick=quick),
    }
    if not skip_external_slow:
        suite["geekbench6"] = run_geekbench6_cli()
        suite["cinebench2024"] = run_cinebench_cli(quick=quick)
    return suite
