from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

import duckdb
import psutil
import zstandard as zstd

from .common import SCRATCH_DIR, find_binary, is_macos, is_windows, run_cmd


def _check_disk_headroom(target_dir: Path, required_gb: float, safety_buffer_gb: float = 7.0) -> tuple[bool, float]:
    target_dir.mkdir(parents=True, exist_ok=True)
    free_gb = psutil.disk_usage(str(target_dir)).free / (1024**3)
    return (free_gb >= (required_gb + safety_buffer_gb)), round(free_gb, 2)


def run_fio_suite(size_gb: int = 8, runtime_sec: int = 15) -> dict[str, Any]:
    fio_bin = find_binary(
        ["fio"],
        mac_candidates=["/opt/homebrew/bin/fio", "/usr/local/bin/fio"],
        win_candidates=[r"C:\Program Files\fio\fio.exe"],
    )
    if not fio_bin:
        hint = "brew install fio" if is_macos() else "winget install fio.fio"
        return {"status": "skipped", "reason": f"fio not installed ({hint})"}

    ok, free_gb = _check_disk_headroom(SCRATCH_DIR, required_gb=float(size_gb), safety_buffer_gb=6.0)
    if not ok:
        return {
            "status": "skipped",
            "reason": f"Insufficient SSD headroom ({free_gb} GiB free; need {size_gb + 6} GiB)",
        }

    fio_dir = SCRATCH_DIR / "fio_run"
    fio_dir.mkdir(parents=True, exist_ok=True)
    test_file = fio_dir / "fio_nvme_test.bin"

    ioengine = "posixaio" if is_macos() else ("windowsaio" if is_windows() else "libaio")
    # On macOS fio maps --direct=1 to fcntl(F_NOCACHE); on Windows --direct=1 maps to FILE_FLAG_NO_BUFFERING
    cache_flags = ["--direct=1"] if is_macos() else ["--direct=1", "--thread=1"]

    jobs = [
        ("seq_read_1m_qd8", "read", "1M", 8),
        ("seq_write_1m_qd8", "write", "1M", 8),
        ("rand_read_4k_qd1", "randread", "4k", 1),
        ("rand_write_4k_qd1", "randwrite", "4k", 1),
        ("rand_read_4k_qd32", "randread", "4k", 32),
    ]

    metrics: dict[str, Any] = {}
    try:
        for job_name, rw, bs, iodepth in jobs:
            cmd = [
                fio_bin,
                f"--name={job_name}",
                f"--filename={test_file}",
                f"--size={size_gb}G",
                f"--ioengine={ioengine}",
                f"--rw={rw}",
                f"--bs={bs}",
                f"--iodepth={iodepth}",
                "--numjobs=1",
                "--time_based=1",
                f"--runtime={runtime_sec}",
                "--group_reporting=1",
                "--output-format=json",
                *cache_flags,
            ]
            proc = run_cmd(cmd, timeout=runtime_sec * 4 + 120)
            if proc.returncode != 0:
                metrics[job_name] = {"status": "error", "stderr": (proc.stderr or "")[-300:]}
                continue

            # Extract JSON block (fio sometimes prints warnings prior to '{')
            stdout = proc.stdout or ""
            brace_idx = stdout.find("{")
            if brace_idx == -1:
                metrics[job_name] = {"status": "error", "reason": "No JSON in fio output"}
                continue

            payload = json.loads(stdout[brace_idx:])
            job_entry = payload["jobs"][0]
            op_key = "read" if "read" in rw else "write"
            op_data = job_entry[op_key]
            bw_mb_s = round(op_data.get("bw_bytes", 0) / (1024 * 1024), 2)
            iops = round(op_data.get("iops", 0), 1)
            clat_ns = op_data.get("clat_ns", {}).get("mean", 0.0)
            metrics[job_name] = {
                "bw_mb_s": bw_mb_s,
                "iops": iops,
                "mean_lat_us": round(clat_ns / 1000.0, 2) if clat_ns else None,
            }
    finally:
        shutil.rmtree(fio_dir, ignore_errors=True)

    return {
        "status": "ok",
        "test_file_gb": size_gb,
        "runtime_per_job_sec": runtime_sec,
        "jobs": metrics,
    }


def run_duckdb_zstd_pipeline(row_millions: int = 80, memory_limit_gb: int = 4) -> dict[str, Any]:
    """
    Mixed CPU + NVMe out-of-core benchmark:
    1) Generate deterministic synthetic telemetry to uncompressed/snappy Parquet shards on SSD (~10-12 GB).
    2) Run a 4GB-RAM-capped out-of-core hash join + window sort forcing NVMe temp spill (~16-18 GB peak).
    3) Stream-recompress output + input shards via multi-threaded zstd (level 6) + SHA-256 verification.
    """
    estimated_peak_gb = max(6.0, row_millions * 0.22)
    ok, free_gb = _check_disk_headroom(SCRATCH_DIR, required_gb=estimated_peak_gb, safety_buffer_gb=6.0)
    if not ok:
        return {
            "status": "skipped",
            "reason": (
                f"Insufficient SSD headroom ({free_gb} GiB free; need ~{round(estimated_peak_gb + 6, 1)} GiB "
                "for clean out-of-core spill)"
            ),
        }

    work_dir = SCRATCH_DIR / "duckdb_pipeline"
    spill_dir = work_dir / "spill"
    parquet_dir = work_dir / "events_parquet"
    out_parquet = work_dir / "ranked_output.parquet"
    zstd_archive = work_dir / "consolidated.tar.zst"

    shutil.rmtree(work_dir, ignore_errors=True)
    spill_dir.mkdir(parents=True, exist_ok=True)
    parquet_dir.mkdir(parents=True, exist_ok=True)

    total_rows = row_millions * 1_000_000

    try:
        con = duckdb.connect(database=":memory:")
        con.execute(f"SET memory_limit = '{memory_limit_gb}GB';")
        con.execute(f"SET temp_directory = '{spill_dir.as_posix()}';")
        con.execute("SET preserve_insertion_order = false;")

        # Phase 1: Deterministic high-entropy dataset generation & SSD write
        t0 = time.perf_counter()
        con.execute(
            f"""
            COPY (
                SELECT
                    i AS event_id,
                    CAST(hash(i) % 100000 AS BIGINT) AS device_id,
                    i % 256 AS shard_bucket,
                    sin(i * 0.00017) * 1000.0 AS sensor_val_a,
                    cos(i * 0.00031) * 500.0 AS sensor_val_b,
                    md5(CAST(i AS VARCHAR)) || md5(CAST(i + 7 AS VARCHAR)) AS payload_hex
                FROM range(0, {total_rows}) t(i)
            ) TO '{parquet_dir.as_posix()}' (FORMAT PARQUET, COMPRESSION UNCOMPRESSED, PER_THREAD_OUTPUT true);
            """
        )
        write_sec = time.perf_counter() - t0
        written_bytes = sum(f.stat().st_size for f in parquet_dir.rglob("*") if f.is_file())
        written_gb = written_bytes / (1024**3)
        write_mb_s = (written_bytes / (1024 * 1024)) / max(write_sec, 1e-6)

        # Phase 2: Out-of-core hash-join + partitioned window sort forcing SSD temp spill
        phase2_sql = f"""
            COPY (
                WITH base AS (
                    SELECT * FROM read_parquet('{(parquet_dir / "*.parquet").as_posix()}')
                ),
                device_stats AS (
                    SELECT
                        device_id,
                        AVG(sensor_val_a) AS mean_a,
                        STDDEV_SAMP(sensor_val_b) AS std_b,
                        COUNT(*) AS cnt
                    FROM base
                    GROUP BY device_id
                ),
                ranked AS (
                    SELECT
                        b.event_id,
                        b.device_id,
                        b.shard_bucket,
                        (b.sensor_val_a - d.mean_a) / NULLIF(d.std_b, 0) AS z_score,
                        b.payload_hex,
                        ROW_NUMBER() OVER (
                            PARTITION BY b.shard_bucket
                            ORDER BY b.sensor_val_a DESC, b.payload_hex ASC
                        ) AS rn
                    FROM base b
                    INNER JOIN device_stats d ON b.device_id = d.device_id
                )
                SELECT * FROM ranked WHERE rn <= 25000
            ) TO '{out_parquet.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD);
            """
        effective_cap_gb = memory_limit_gb
        t1 = time.perf_counter()
        try:
            con.execute(phase2_sql)
        except duckdb.Error as exc:
            if "Out of buffer" not in str(exc):
                raise
            # DuckDB 2.0 on 12-thread Windows can exhaust 4GB buffer pool when flushing ZSTD row groups;
            # bump cap to 5GB (still forces ~16GB+ SSD spill on 80M rows) and retry Phase 2.
            shutil.rmtree(spill_dir, ignore_errors=True)
            spill_dir.mkdir(parents=True, exist_ok=True)
            out_parquet.unlink(missing_ok=True)
            effective_cap_gb = memory_limit_gb + 1
            con.execute(f"SET memory_limit = '{effective_cap_gb}GB';")
            t1 = time.perf_counter()
            con.execute(phase2_sql)
        spill_query_sec = time.perf_counter() - t1
        con.close()

        # Phase 3: Parallel multi-threaded zstd read -> compress -> SSD write + SHA-256 verification
        t2 = time.perf_counter()
        cctx = zstd.ZstdCompressor(level=6, threads=-1)
        hasher = hashlib.sha256()
        source_files = sorted(parquet_dir.rglob("*.parquet")) + ([out_parquet] if out_parquet.exists() else [])
        raw_stream_bytes = 0

        with zstd_archive.open("wb") as fout:
            with cctx.stream_writer(fout, closefd=False) as compressor:
                for src in source_files:
                    with src.open("rb") as fin:
                        while True:
                            chunk = fin.read(4 * 1024 * 1024)
                            if not chunk:
                                break
                            raw_stream_bytes += len(chunk)
                            hasher.update(chunk)
                            compressor.write(chunk)

        recompress_sec = time.perf_counter() - t2
        compressed_bytes = zstd_archive.stat().st_size if zstd_archive.exists() else 0
        recompress_mb_s = (raw_stream_bytes / (1024 * 1024)) / max(recompress_sec, 1e-6)
        total_sec = write_sec + spill_query_sec + recompress_sec

        return {
            "status": "ok",
            "rows_millions": row_millions,
            "duckdb_ram_cap_gb": effective_cap_gb,
            "dataset_written_gb": round(written_gb, 2),
            "phase1_ingest_write_sec": round(write_sec, 2),
            "phase1_ingest_mb_s": round(write_mb_s, 1),
            "phase2_out_of_core_spill_sec": round(spill_query_sec, 2),
            "phase2_rows_per_sec_millions": round(row_millions / max(spill_query_sec, 1e-6), 2),
            "phase3_zstd_recompress_sec": round(recompress_sec, 2),
            "phase3_stream_mb_s": round(recompress_mb_s, 1),
            "compressed_output_gb": round(compressed_bytes / (1024**3), 2),
            "total_pipeline_sec": round(total_sec, 2),
            "sha256_prefix": hasher.hexdigest()[:16],
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def run_storage_compute_suite(quick: bool = False) -> dict[str, Any]:
    fio_size = 2 if quick else 8
    fio_runtime = 5 if quick else 15
    rows_m = 15 if quick else 80
    return {
        "fio_nvme": run_fio_suite(size_gb=fio_size, runtime_sec=fio_runtime),
        "duckdb_zstd_out_of_core": run_duckdb_zstd_pipeline(row_millions=rows_m, memory_limit_gb=4),
    }
