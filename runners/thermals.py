from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import get_external_cache_dir, is_macos, is_windows, win_subprocess_kwargs


def _find_lhm_dll() -> Path | None:
    env_path = os.environ.get("RIG_BENCH_LHM_DLL")
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_file():
            return p

    candidates: list[Path] = [
        get_external_cache_dir() / "tools" / "LibreHardwareMonitor" / "LibreHardwareMonitorLib.dll",
        Path(r"C:\Program Files\FanControl\LibreHardwareMonitorLib.dll"),
        Path(r"C:\Program Files\LibreHardwareMonitor\LibreHardwareMonitorLib.dll"),
        Path.home() / "Tools" / "FanControl" / "LibreHardwareMonitorLib.dll",
        Path.home() / "Tools" / "LibreHardwareMonitor" / "LibreHardwareMonitorLib.dll",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _summarize_series(values: list[float], unit_decimals: int = 1) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": round(min(values), unit_decimals),
        "avg": round(sum(values) / len(values), unit_decimals),
        "max": round(max(values), unit_decimals),
        "end": round(values[-1], unit_decimals),
    }


@dataclass
class ThermalMonitor:
    """Background thermal, fan, and GPU power sampler active during benchmark execution."""

    interval_sec: float = 4.0
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None
    _ps_proc: subprocess.Popen[str] | None = None
    _ps_reader: threading.Thread | None = None
    _latest_ps_sample: dict[str, Any] = field(default_factory=dict)
    _current_suite: str = "idle"
    _samples: list[dict[str, Any]] = field(default_factory=list)
    _backends: set[str] = field(default_factory=set)

    def set_suite(self, suite_name: str) -> None:
        with self._lock:
            self._current_suite = suite_name

    def start(self) -> None:
        if is_windows():
            self._start_windows_helper()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="rig-bench-thermals")
        self._thread.start()
        # Take one immediate baseline sample
        self._record_once()

    def stop(self) -> dict[str, Any]:
        self._record_once()
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._ps_proc:
            try:
                self._ps_proc.terminate()
                self._ps_proc.wait(timeout=3)
            except Exception:
                try:
                    self._ps_proc.kill()
                except Exception:
                    pass
        return self.summarize()

    def _start_windows_helper(self) -> None:
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            return
        lhm_dll = _find_lhm_dll()
        if lhm_dll:
            hid_dll = lhm_dll.parent / "HidSharp.dll"
            self._backends.add("LibreHardwareMonitorLib")
            ps_script = f"""
$ErrorActionPreference = 'SilentlyContinue'
if (Test-Path '{hid_dll}') {{ Add-Type -Path '{hid_dll}' }}
Add-Type -Path '{lhm_dll}'
$comp = New-Object LibreHardwareMonitor.Hardware.Computer
$comp.IsCpuEnabled = $true
$comp.IsMotherboardEnabled = $true
$comp.IsStorageEnabled = $true
$comp.IsGpuEnabled = $true
$comp.Open()
try {{
    while ($true) {{
        $out = @{{}}
        foreach ($h in $comp.Hardware) {{
            $h.Update()
            foreach ($sh in $h.SubHardware) {{
                $sh.Update()
                foreach ($s in $sh.Sensors) {{
                    if ($null -eq $s.Value) {{ continue }}
                    if ($s.SensorType -eq 'Fan' -and $s.Name -match 'CPU Fan' -and -not $out.ContainsKey('cpu_fan_rpm')) {{ $out['cpu_fan_rpm'] = [math]::Round($s.Value, 0) }}
                    if ($s.SensorType -eq 'Fan' -and $s.Name -match 'Chassis #1' -and $s.Value -gt 0) {{ $out['chassis_fan_rpm'] = [math]::Round($s.Value, 0) }}
                }}
            }}
            foreach ($s in $h.Sensors) {{
                if ($null -eq $s.Value) {{ continue }}
                if ($h.HardwareType -eq 'Cpu' -and $s.SensorType -eq 'Temperature') {{
                    if ($s.Name -match 'Tctl/Tdie|Package|CCD1' -and -not $out.ContainsKey('cpu_temp_c')) {{
                        $out['cpu_temp_c'] = [math]::Round($s.Value, 1)
                        $out['cpu_sensor'] = $s.Name
                    }}
                }}
                if ($h.HardwareType -eq 'Storage' -and $s.SensorType -eq 'Temperature' -and $s.Name -eq 'Temperature' -and -not $out.ContainsKey('ssd_temp_c')) {{
                    $out['ssd_temp_c'] = [math]::Round($s.Value, 1)
                    $out['ssd_sensor'] = $h.Name
                }}
                if (($h.HardwareType -match 'Gpu') -and $s.SensorType -eq 'Temperature') {{
                    if ($s.Name -eq 'GPU Core' -and -not $out.ContainsKey('gpu_temp_c')) {{ $out['gpu_temp_c'] = [math]::Round($s.Value, 1) }}
                    if ($s.Name -eq 'GPU Hot Spot' -and -not $out.ContainsKey('gpu_hotspot_c')) {{ $out['gpu_hotspot_c'] = [math]::Round($s.Value, 1) }}
                }}
            }}
        }}
        Write-Output ($out | ConvertTo-Json -Compress)
        Start-Sleep -Seconds {int(max(2, self.interval_sec))}
    }}
}} finally {{
    $comp.Close()
}}
"""
        else:
            self._backends.add("WindowsStorageReliabilityCounter")
            ps_script = f"""
$ErrorActionPreference = 'SilentlyContinue'
while ($true) {{
    $out = @{{}}
    $disk = Get-PhysicalDisk | Where-Object MediaType -ne 'Unspecified' | Select-Object -First 1
    if ($disk) {{
        $rel = $disk | Get-StorageReliabilityCounter
        if ($rel -and $null -ne $rel.Temperature) {{
            $out['ssd_temp_c'] = [math]::Round($rel.Temperature, 1)
            $out['ssd_sensor'] = $disk.FriendlyName
        }}
    }}
    Write-Output ($out | ConvertTo-Json -Compress)
    Start-Sleep -Seconds {int(max(2, self.interval_sec))}
}}
"""
        import base64

        b64 = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
        self._ps_proc = subprocess.Popen(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", b64],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            **win_subprocess_kwargs(),
        )
        self._ps_reader = threading.Thread(target=self._read_ps_stdout, daemon=True)
        self._ps_reader.start()

    def _read_ps_stdout(self) -> None:
        if not self._ps_proc or not self._ps_proc.stdout:
            return
        for line in self._ps_proc.stdout:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    with self._lock:
                        self._latest_ps_sample = parsed
            except Exception:
                pass

    def _sample_nvidia_smi(self) -> dict[str, Any]:
        nv = shutil.which("nvidia-smi")
        if not nv:
            return {}
        try:
            proc = subprocess.run(
                [
                    nv,
                    "--query-gpu=temperature.gpu,power.draw,clocks.gr,clocks.mem,fan.speed",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                **win_subprocess_kwargs(),
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return {}
            self._backends.add("nvidia-smi")
            parts = [p.strip() for p in proc.stdout.splitlines()[0].split(",")]
            out: dict[str, Any] = {}
            for idx, key in enumerate(("gpu_temp_c", "gpu_power_w", "gpu_clock_mhz", "gpu_mem_clock_mhz", "gpu_fan_pct")):
                if idx < len(parts):
                    try:
                        out[key] = float(parts[idx])
                    except ValueError:
                        pass
            return out
        except Exception:
            return {}

    def _sample_macos(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            pm = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=3, check=False)
            if pm.returncode == 0:
                self._backends.add("pmset-therm")
                m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", pm.stdout)
                if m:
                    limit = int(m.group(1))
                    out["cpu_speed_limit_pct"] = limit
                    if limit < 100:
                        out["throttled"] = True
        except Exception:
            pass

        smartctl = shutil.which("smartctl") or ("/opt/homebrew/bin/smartctl" if Path("/opt/homebrew/bin/smartctl").is_file() else None)
        if smartctl:
            try:
                sc = subprocess.run([smartctl, "-j", "-a", "/dev/disk0"], capture_output=True, text=True, timeout=4, check=False)
                if sc.stdout.strip().startswith("{"):
                    data = json.loads(sc.stdout)
                    temp = data.get("temperature", {}).get("current")
                    if isinstance(temp, (int, float)):
                        self._backends.add("smartctl-nvme")
                        out["ssd_temp_c"] = float(temp)
                        out["ssd_sensor"] = data.get("model_name", "Apple Internal NVMe")
            except Exception:
                pass

        osx_temp = shutil.which("osx-cpu-temp")
        if osx_temp:
            try:
                ot = subprocess.run([osx_temp], capture_output=True, text=True, timeout=3, check=False)
                m = re.search(r"([\d.]+)\s*°?C", ot.stdout)
                if m and float(m.group(1)) > 0:
                    self._backends.add("osx-cpu-temp")
                    out["cpu_temp_c"] = float(m.group(1))
                    out["cpu_sensor"] = "osx-cpu-temp"
            except Exception:
                pass
        return out

    def _sample_linux(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        hwmon_root = Path("/sys/class/hwmon")
        if hwmon_root.is_dir():
            for hw in sorted(hwmon_root.iterdir()):
                name_file = hw / "name"
                if not name_file.is_file():
                    continue
                drv = name_file.read_text(encoding="utf-8", errors="ignore").strip().lower()
                for tfile in sorted(hw.glob("temp*_input")):
                    try:
                        val_c = round(int(tfile.read_text().strip()) / 1000.0, 1)
                    except Exception:
                        continue
                    if val_c <= 0 or val_c > 150:
                        continue
                    if drv in ("k10temp", "coretemp", "zenpower") and "cpu_temp_c" not in out:
                        self._backends.add(f"hwmon:{drv}")
                        out["cpu_temp_c"] = val_c
                        out["cpu_sensor"] = drv
                    elif "nvme" in drv and "ssd_temp_c" not in out:
                        self._backends.add("hwmon:nvme")
                        out["ssd_temp_c"] = val_c
                        out["ssd_sensor"] = drv
        return out

    def _record_once(self) -> None:
        sample: dict[str, Any] = {"ts": time.time()}
        if is_windows():
            with self._lock:
                sample.update(self._latest_ps_sample)
            sample.update({k: v for k, v in self._sample_nvidia_smi().items() if v is not None})
        elif is_macos():
            sample.update(self._sample_macos())
        else:
            sample.update(self._sample_linux())
            sample.update({k: v for k, v in self._sample_nvidia_smi().items() if v is not None})

        with self._lock:
            sample["suite"] = self._current_suite
            self._samples.append(sample)

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self.interval_sec):
            self._record_once()

    def summarize(self) -> dict[str, Any]:
        with self._lock:
            samples = list(self._samples)
            backends = sorted(self._backends)

        def _collect(key: str, suite_filter: str | None = None) -> list[float]:
            vals = []
            for s in samples:
                if suite_filter and s.get("suite") != suite_filter:
                    continue
                v = s.get(key)
                if isinstance(v, (int, float)):
                    vals.append(float(v))
            return vals

        overall: dict[str, Any] = {}
        for key in ("cpu_temp_c", "gpu_temp_c", "gpu_hotspot_c", "ssd_temp_c", "gpu_power_w", "gpu_clock_mhz", "cpu_fan_rpm", "chassis_fan_rpm"):
            summary = _summarize_series(_collect(key))
            if summary:
                overall[key] = summary

        for s in reversed(samples):
            if "cpu_sensor" in s and "cpu_sensor" not in overall:
                overall["cpu_sensor"] = s["cpu_sensor"]
            if "ssd_sensor" in s and "ssd_sensor" not in overall:
                overall["ssd_sensor"] = s["ssd_sensor"]

        per_suite: dict[str, Any] = {}
        for suite_name in ("cpu", "gpu_raster", "gpu_compute", "storage_compute"):
            suite_obj: dict[str, Any] = {}
            for key in ("cpu_temp_c", "gpu_temp_c", "ssd_temp_c", "gpu_power_w"):
                vals = _collect(key, suite_filter=suite_name)
                if vals:
                    suite_obj[key] = _summarize_series(vals)
            if suite_obj:
                per_suite[suite_name] = suite_obj

        throttled = any(bool(s.get("throttled")) for s in samples)
        return {
            "sample_count": len(samples),
            "interval_sec": self.interval_sec,
            "backends_used": backends,
            "thermal_throttle_detected": throttled,
            "overall": overall,
            "per_suite": per_suite,
        }
