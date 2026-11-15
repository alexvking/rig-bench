#!/usr/bin/env python3
from __future__ import annotations

import functools
import io
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from bench import (
    LiveManifestHandler,
    ReusableThreadingHTTPServer,
    build_manifest,
    extract_flat_metrics,
    is_full_complete_run,
)
from runners.common import (
    DOCS_DIR,
    REPO_ROOT,
    RESULTS_DIR,
    SCRATCH_DIR,
    ensure_frozen_docs,
    find_binary,
    get_external_cache_dir,
    is_macos,
    is_windows,
)
from runners.cpu import run_cpu_suite
from runners.gpu_compute import MODEL_SPECS, run_gpu_compute_suite
from runners.gpu_raster import run_gpu_raster_suite
from runners.storage_compute import run_storage_compute_suite
from runners.sysinfo import collect_sysinfo
from runners.thermals import ThermalMonitor

BG = "#0b0f17"
PANEL = "#121826"
PANEL_ALT = "#182033"
BORDER = "#253047"
TEXT = "#e6edf7"
MUTED = "#8b9bb4"
CYAN = "#38bdf8"
EMERALD = "#34d399"
AMBER = "#fbbf24"
ROSE = "#fb7185"


def _pick_open_port(start: int = 8788, end: int = 8799) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def _download_latest_llama_bench(log_cb) -> bool:
    """Download official llama.cpp Windows release (CUDA if NVIDIA GPU detected, else Vulkan)."""
    if not is_windows():
        return False
    has_nvidia = bool(shutil.which("nvidia-smi"))
    suffix = "bin-win-cuda-cu12.4-x64.zip" if has_nvidia else "bin-win-vulkan-x64.zip"
    api_url = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    log_cb(f"Querying latest llama.cpp release ({'CUDA 12.4' if has_nvidia else 'Vulkan'})...")
    req = urllib.request.Request(api_url, headers={"User-Agent": "rig-bench-gui/0.3"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        rel = json.loads(resp.read().decode("utf-8"))

    assets = rel.get("assets", [])
    targets = [a for a in assets if a.get("name", "").endswith(suffix)]
    if has_nvidia:
        targets += [a for a in assets if "cudart-llama-bin-win-cu12.4-x64.zip" in a.get("name", "")]

    if not targets:
        log_cb(f"Could not find matching llama.cpp asset ending in {suffix}.")
        return False

    dest_dir = get_external_cache_dir() / "tools" / "llama.cpp"
    dest_dir.mkdir(parents=True, exist_ok=True)

    for asset in targets:
        name = asset["name"]
        url = asset["browser_download_url"]
        zip_path = dest_dir / name
        log_cb(f"Downloading {name}...")
        dl_req = urllib.request.Request(url, headers={"User-Agent": "rig-bench-gui/0.3"})
        with urllib.request.urlopen(dl_req, timeout=120) as r, zip_path.open("wb") as f:
            shutil.copyfileobj(r, f)
        log_cb(f"Extracting {name} -> {dest_dir}...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(dest_dir)
        zip_path.unlink(missing_ok=True)
    return bool(find_binary(["llama-bench"]))


class RigBenchApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("rig-bench — Cross-Platform Compute, GPU, NVMe & Thermal Benchmark")
        self.root.geometry("940x700")
        self.root.minsize(820, 600)
        self.root.configure(bg=BG)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.running = False
        self.dashboard_port = 8788
        self.sysinfo: dict[str, Any] = {}

        ensure_frozen_docs()
        build_manifest()
        self._start_http_server()
        self._build_ui()
        self.root.after(100, self._drain_logs)
        self._refresh_status_async()

    def _start_http_server(self) -> None:
        self.dashboard_port = _pick_open_port(8788, 8799)
        handler = functools.partial(LiveManifestHandler, directory=str(DOCS_DIR))
        server = ReusableThreadingHTTPServer(("127.0.0.1", self.dashboard_port), handler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="rig-bench-http")
        t.start()

    def _build_ui(self) -> None:
        pad = {"padx": 14, "pady": 8}

        # Header
        hdr = tk.Frame(self.root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        hdr.pack(fill="x", **pad)
        tk.Label(hdr, text="RIG-BENCH", fg=CYAN, bg=PANEL, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(8, 0))
        self.lbl_hw = tk.Label(hdr, text="Detecting hardware...", fg=TEXT, bg=PANEL, font=("Segoe UI", 13, "bold"))
        self.lbl_hw.pack(anchor="w", padx=12)
        self.lbl_sub = tk.Label(hdr, text="Initializing...", fg=MUTED, bg=PANEL, font=("Segoe UI", 9))
        self.lbl_sub.pack(anchor="w", padx=12, pady=(0, 8))

        # Tool Readiness + Thermals
        info_row = tk.Frame(self.root, bg=BG)
        info_row.pack(fill="x", padx=14)
        self.lbl_tools = tk.Label(info_row, text="Tools: checking...", fg=MUTED, bg=BG, font=("Consolas", 9), anchor="w", justify="left")
        self.lbl_tools.pack(side="left", fill="x", expand=True)
        self.lbl_thermals = tk.Label(info_row, text="Thermals: idle", fg=EMERALD, bg=BG, font=("Consolas", 9, "bold"))
        self.lbl_thermals.pack(side="right")

        # Controls Panel
        ctrl = tk.Frame(self.root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        ctrl.pack(fill="x", **pad)

        row1 = tk.Frame(ctrl, bg=PANEL)
        row1.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(row1, text="Profile ID:", fg=MUTED, bg=PANEL, font=("Segoe UI", 9)).pack(side="left")
        self.var_profile = tk.StringVar(value="stock")
        tk.Entry(row1, textvariable=self.var_profile, width=18, bg=PANEL_ALT, fg=TEXT, insertbackground=TEXT, relief="flat").pack(side="left", padx=(6, 14), ipady=3)

        tk.Label(row1, text="Notes:", fg=MUTED, bg=PANEL, font=("Segoe UI", 9)).pack(side="left")
        self.var_notes = tk.StringVar(value="Stock BIOS baseline")
        tk.Entry(row1, textvariable=self.var_notes, width=34, bg=PANEL_ALT, fg=TEXT, insertbackground=TEXT, relief="flat").pack(side="left", padx=(6, 14), ipady=3, fill="x", expand=True)

        self.var_quick = tk.BooleanVar(value=False)
        tk.Checkbutton(row1, text="Quick Smoke (~2-3m)", variable=self.var_quick, bg=PANEL, fg=TEXT, selectcolor=PANEL_ALT, activebackground=PANEL, activeforeground=TEXT).pack(side="right")

        row2 = tk.Frame(ctrl, bg=PANEL)
        row2.pack(fill="x", padx=12, pady=(4, 10))

        self.btn_run = tk.Button(row2, text="Run Full Benchmark", command=self._on_run, bg="#0284c7", fg="#ffffff", font=("Segoe UI", 10, "bold"), relief="flat", padx=14, pady=6, cursor="hand2")
        self.btn_run.pack(side="left", padx=(0, 8))

        self.btn_boot = tk.Button(row2, text="Install Missing Tools", command=self._on_bootstrap, bg=PANEL_ALT, fg=TEXT, font=("Segoe UI", 9, "bold"), relief="flat", padx=10, pady=6, cursor="hand2")
        self.btn_boot.pack(side="left", padx=(0, 8))

        self.var_dl_model = tk.BooleanVar(value=True)
        tk.Checkbutton(row2, text="+ 7B LLM (4.7 GB)", variable=self.var_dl_model, bg=PANEL, fg=MUTED, selectcolor=PANEL_ALT, activebackground=PANEL, activeforeground=TEXT).pack(side="left", padx=(0, 12))

        tk.Button(row2, text="Open Web UI", command=self._open_dashboard, bg="#059669", fg="#ffffff", font=("Segoe UI", 9, "bold"), relief="flat", padx=12, pady=6, cursor="hand2").pack(side="right")
        tk.Button(row2, text="Import JSON", command=self._import_json, bg=PANEL_ALT, fg=TEXT, font=("Segoe UI", 9), relief="flat", padx=10, pady=6, cursor="hand2").pack(side="right", padx=(0, 8))
        tk.Button(row2, text="Data Folder", command=self._open_data_dir, bg=PANEL_ALT, fg=TEXT, font=("Segoe UI", 9), relief="flat", padx=10, pady=6, cursor="hand2").pack(side="right", padx=(0, 8))

        # Log Output
        log_frame = tk.Frame(self.root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        log_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.txt_log = scrolledtext.ScrolledText(
            log_frame,
            bg="#090d14",
            fg=TEXT,
            insertbackground=TEXT,
            font=("Consolas", 9),
            relief="flat",
            wrap="word",
        )
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=6)
        self.log(f"Data directory: {REPO_ROOT}")
        self.log(f"Web UI ready at http://127.0.0.1:{self.dashboard_port}/")

    def log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {msg}\n")

    def _drain_logs(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.txt_log.insert("end", line)
            self.txt_log.see("end")
        self.root.after(120, self._drain_logs)

    def _refresh_status_async(self) -> None:
        def worker() -> None:
            info = collect_sysinfo(scratch_target=SCRATCH_DIR)
            self.sysinfo = info
            tools = {
                "7-Zip": bool(find_binary(["7zz", "7z"], ["/opt/homebrew/bin/7zz"], [r"C:\Program Files\7-Zip\7z.exe"])),
                "fio": bool(find_binary(["fio"], ["/opt/homebrew/bin/fio"], [r"C:\Program Files\fio\fio.exe"])),
                "Blender": bool(find_binary(["blender", "Blender"], ["/Applications/Blender.app/Contents/MacOS/Blender"], [r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe", r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe", r"C:\Program Files\Blender Foundation\Blender 5.3\blender.exe"])),
                "llama-bench": bool(find_binary(["llama-bench"], ["/opt/homebrew/bin/llama-bench"])),
                "uv": bool(shutil.which("uv")),
            }
            ram_spd = f" @ {info['ram_speed_mt_s']} MT/s" if info.get("ram_speed_mt_s") else ""
            hw_title = f"{info.get('cpu_model', 'Unknown CPU')}  |  {info.get('gpu_model', 'Unknown GPU')}"
            sub = (
                f"Machine ID: {info.get('machine_id')}  ·  RAM: {info.get('ram_total_gb')} GiB{ram_spd}  ·  "
                f"SSD: {info.get('ssd_model')} ({info.get('ssd_free_gb_at_start')} GiB free)"
            )
            tool_str = "Tools: " + "  ".join(f"[{'OK' if ok else 'MISSING'}] {name}" for name, ok in tools.items())
            self.root.after(0, lambda: self.lbl_hw.configure(text=hw_title))
            self.root.after(0, lambda: self.lbl_sub.configure(text=sub))
            self.root.after(0, lambda: self.lbl_tools.configure(text=tool_str))

        threading.Thread(target=worker, daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        self.running = busy
        state = "disabled" if busy else "normal"
        self.btn_run.configure(state=state, text="Running..." if busy else "Run Full Benchmark")
        self.btn_boot.configure(state=state)

    def _on_bootstrap(self) -> None:
        if self.running:
            return
        self._set_busy(True)
        dl_model = self.var_dl_model.get()

        def worker() -> None:
            try:
                if is_windows():
                    for pkg in ("astral-sh.uv", "7zip.7zip", "fio.fio", "BlenderFoundation.Blender"):
                        self.log(f"Installing/verifying {pkg} via winget...")
                        subprocess.run(
                            ["winget", "install", "--id", pkg, "-e", "--accept-source-agreements", "--accept-package-agreements"],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    if not find_binary(["llama-bench"]):
                        ok = _download_latest_llama_bench(self.log)
                        self.log("llama-bench ready!" if ok else "llama-bench auto-download failed.")
                elif is_macos():
                    self.log("On macOS, run: brew install sevenzip fio llama.cpp")

                if dl_model:
                    models_dir = get_external_cache_dir() / "models"
                    models_dir.mkdir(parents=True, exist_ok=True)
                    spec = MODEL_SPECS[0]
                    dest = models_dir / spec["filename"]
                    if dest.is_file() and dest.stat().st_size > 100_000_000:
                        self.log(f"Model already cached: {dest.name}")
                    else:
                        self.log(f"Downloading {spec['label']} (~4.7 GB)...")
                        req = urllib.request.Request(spec["hf_urls"][0], headers={"User-Agent": "rig-bench-gui/0.3"})
                        tmp = dest.with_suffix(".part")
                        with urllib.request.urlopen(req, timeout=300) as r, tmp.open("wb") as f:
                            shutil.copyfileobj(r, f)
                        tmp.replace(dest)
                        self.log(f"Model ready: {dest}")
                self.log("Bootstrap finished.")
            except Exception as exc:
                self.log(f"Bootstrap error: {exc}")
            finally:
                self._refresh_status_async()
                self.root.after(0, lambda: self._set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def _on_run(self) -> None:
        if self.running:
            return
        self._set_busy(True)
        quick = self.var_quick.get()
        profile_id = self.var_profile.get().strip() or None
        notes = self.var_notes.get().strip()

        def worker() -> None:
            monitor = ThermalMonitor(interval_sec=4.0)
            stop_ui_poll = threading.Event()

            def _poll_thermal_label() -> None:
                while not stop_ui_poll.wait(4.0):
                    s = monitor.summarize().get("overall", {})
                    parts = []
                    if "cpu_temp_c" in s:
                        parts.append(f"CPU {s['cpu_temp_c']['end']}C (max {s['cpu_temp_c']['max']}C)")
                    if "gpu_temp_c" in s:
                        parts.append(f"GPU {s['gpu_temp_c']['end']}C (max {s['gpu_temp_c']['max']}C)")
                    if "ssd_temp_c" in s:
                        parts.append(f"SSD {s['ssd_temp_c']['end']}C")
                    if parts:
                        txt = "Thermals: " + " | ".join(parts)
                        self.root.after(0, lambda t=txt: self.lbl_thermals.configure(text=t))

            try:
                SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
                sysinfo = collect_sysinfo(scratch_target=SCRATCH_DIR)
                mid = sysinfo["machine_id"]
                self.log(f"Starting {'QUICK SMOKE' if quick else 'FULL'} run on {sysinfo['display_name']} (profile={profile_id or mid})...")

                suites_out: dict[str, Any] = {}
                t_start = datetime.now(timezone.utc)
                monitor.start()
                threading.Thread(target=_poll_thermal_label, daemon=True).start()

                for name, fn in (
                    ("cpu", lambda: run_cpu_suite(quick=quick, skip_external_slow=False)),
                    ("gpu_raster", lambda: run_gpu_raster_suite(quick=quick)),
                    ("gpu_compute", lambda: run_gpu_compute_suite(quick=quick)),
                    ("storage_compute", lambda: run_storage_compute_suite(quick=quick)),
                ):
                    self.log(f"-> Running {name} suite...")
                    monitor.set_suite(name)
                    suites_out[name] = fn()
                    self.log(f"   {name} complete.")

                stop_ui_poll.set()
                thermals_summary = monitor.stop()
                t_end = datetime.now(timezone.utc)

                doc: dict[str, Any] = {
                    "schema_version": "1.0",
                    "timestamp_utc": t_end.isoformat(),
                    "duration_sec": round((t_end - t_start).total_seconds(), 2),
                    "quick_mode": bool(quick),
                    "notes": notes,
                    "sysinfo": sysinfo,
                    "thermals": thermals_summary,
                    "suites": suites_out,
                }
                if profile_id:
                    doc["profile_id"] = profile_id
                doc["flat_metrics"] = extract_flat_metrics(suites_out)

                out_dir = RESULTS_DIR / mid
                out_dir.mkdir(parents=True, exist_ok=True)
                out_file = out_dir / f"{t_end.strftime('%Y%m%d_%H%M%S')}.json"
                out_file.write_text(json.dumps(doc, indent=2), encoding="utf-8")

                build_manifest()
                self.log(f"Saved run: {out_file} ({len(doc['flat_metrics'])} metrics)")
                if not is_full_complete_run(doc):
                    self.log("Note: Quick/partial runs are saved on disk; only full 4-suite (22-metric) runs publish to the Web UI.")
                else:
                    self.log("Full run published to Web UI! Opening dashboard...")
                    self.root.after(0, self._open_dashboard)
            except Exception as exc:
                stop_ui_poll.set()
                try:
                    monitor.stop()
                except Exception:
                    pass
                self.log(f"ERROR during run: {exc}")
            finally:
                self.root.after(0, lambda: self._set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def _open_dashboard(self) -> None:
        webbrowser.open(f"http://127.0.0.1:{self.dashboard_port}/")

    def _open_data_dir(self) -> None:
        if is_windows():
            os.startfile(str(REPO_ROOT))
        elif is_macos():
            subprocess.run(["open", str(REPO_ROOT)], check=False)

    def _import_json(self) -> None:
        paths = filedialog.askopenfilenames(title="Select rig-bench result JSON(s)", filetypes=[("JSON files", "*.json")])
        if not paths:
            return
        imp_dir = RESULTS_DIR / "imported"
        imp_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for p in paths:
            src = Path(p)
            shutil.copy2(src, imp_dir / src.name)
            count += 1
        build_manifest()
        self.log(f"Imported {count} JSON file(s) and rebuilt manifest. Refresh Web UI to compare.")


def main() -> None:
    root = tk.Tk()
    RigBenchApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
