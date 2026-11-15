from __future__ import annotations

import json
import platform
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from .common import REPO_ROOT, SCRATCH_DIR, is_macos, is_windows, run_cmd


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _default_machine_id(cpu_brand: str, gpu_model: str) -> str:
    combined = f"{cpu_brand} {gpu_model}".lower()
    if "apple m4" in combined and "pro" not in combined and "max" not in combined:
        return "m4-mac-mini"
    if "3600" in combined and "1660" in combined:
        return "zen2-1660s-pc"

    cpu_short = re.sub(r"\b(amd|intel|core|processor|with radeon graphics|\d+-core)\b", " ", cpu_brand, flags=re.I)
    gpu_short = re.sub(r"\b(nvidia|geforce|amd|radeon|apple|graphics|\d+-core gpu)\b", " ", gpu_model, flags=re.I)
    cpu_slug = _slugify(cpu_short)[:24].strip("-")
    gpu_slug = _slugify(gpu_short)[:20].strip("-")
    parts = [p for p in (cpu_slug, gpu_slug) if p]
    if parts:
        return "-".join(parts)
    slug = _slugify(f"{platform.system()}-{platform.machine()}")
    return slug or "unknown-host"


def _load_rig_config_overrides() -> dict[str, Any]:
    cfg_path = REPO_ROOT / "rig-config.json"
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _macos_sysinfo() -> dict[str, Any]:
    cpu_brand = run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=5).stdout.strip() or "Apple Silicon"
    perf_cores = run_cmd(["sysctl", "-n", "hw.perflevel0.physicalcpu"], timeout=5).stdout.strip()
    eff_cores = run_cmd(["sysctl", "-n", "hw.perflevel1.physicalcpu"], timeout=5).stdout.strip()

    gpu_model = cpu_brand
    gpu_cores = None
    metal_version = None
    sp_disp = run_cmd(["system_profiler", "SPDisplaysDataType", "-json"], timeout=10)
    if sp_disp.returncode == 0:
        try:
            data = json.loads(sp_disp.stdout).get("SPDisplaysDataType", [])
            if data:
                first = data[0]
                gpu_model = first.get("sppci_model", gpu_model)
                cores_raw = first.get("sppci_cores")
                gpu_cores = int(cores_raw) if cores_raw and str(cores_raw).isdigit() else None
                metal_version = first.get("spdisplays_mtlgpufamilysupport")
        except Exception:
            pass

    ssd_model = "Internal NVMe"
    sp_nvme = run_cmd(["system_profiler", "SPNVMeDataType", "-json"], timeout=10)
    if sp_nvme.returncode == 0:
        try:
            nvme_groups = json.loads(sp_nvme.stdout).get("SPNVMeDataType", [])
            for grp in nvme_groups:
                items = grp.get("_items", [])
                if items:
                    ssd_model = items[0].get("device_model") or items[0].get("_name") or ssd_model
                    break
        except Exception:
            pass

    return {
        "cpu_model": cpu_brand,
        "cpu_topology_note": f"{perf_cores}P + {eff_cores}E" if perf_cores and eff_cores else None,
        "gpu_model": f"{gpu_model} ({gpu_cores}-core GPU)" if gpu_cores else gpu_model,
        "gpu_vram_gb": round(psutil.virtual_memory().total / (1024**3), 1),
        "gpu_memory_type": "Unified Memory (UMA)",
        "gpu_api_primary": metal_version or "Metal",
        "ssd_model": ssd_model,
        "estimated_peak_watts": 26,
        "idle_wall_watts": 4.5,
        "price_usd": 500,
        "price_note": "Promotional new pricing (late 2025 / early 2026)",
        "workload_wall_watts": {
            "CPU": 26.0,
            "GPU Graphics": 22.0,
            "GPU Math": 22.0,
            "SSD and I/O Compute": 24.0,
        },
    }


def _windows_sysinfo() -> dict[str, Any]:
    cpu_model = platform.processor() or "x86_64 CPU"
    ps_cpu = run_cmd(
        ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"],
        timeout=10,
    )
    if ps_cpu.returncode == 0 and ps_cpu.stdout.strip():
        cpu_model = ps_cpu.stdout.strip()

    gpu_model = "Discrete GPU"
    gpu_vram_gb: float | None = None
    driver_ver: str | None = None

    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        nv = run_cmd(
            [nvidia_smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            timeout=10,
        )
        if nv.returncode == 0 and nv.stdout.strip():
            parts = [p.strip() for p in nv.stdout.splitlines()[0].split(",")]
            if len(parts) >= 3:
                gpu_model = parts[0]
                try:
                    gpu_vram_gb = round(float(parts[1]) / 1024.0, 2)
                except ValueError:
                    pass
                driver_ver = parts[2]
    else:
        ps_gpu = run_cmd(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress",
            ],
            timeout=10,
        )
        if ps_gpu.returncode == 0 and ps_gpu.stdout.strip():
            try:
                parsed = json.loads(ps_gpu.stdout)
                items = parsed if isinstance(parsed, list) else [parsed]
                for item in items:
                    name = item.get("Name", "")
                    if name and "remote" not in name.lower() and "virtual" not in name.lower():
                        gpu_model = name
                        if item.get("AdapterRAM"):
                            gpu_vram_gb = round(float(item["AdapterRAM"]) / (1024**3), 2)
                        break
            except Exception:
                pass

    ssd_model = "System SSD"
    ps_disk = run_cmd(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-PhysicalDisk | Where-Object MediaType -ne 'Unspecified' | Select-Object -First 1).FriendlyName",
        ],
        timeout=10,
    )
    if ps_disk.returncode == 0 and ps_disk.stdout.strip():
        ssd_model = ps_disk.stdout.strip()

    ram_speed_mt_s: int | None = None
    ps_ram = run_cmd(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property ConfiguredClockSpeed -Maximum).Maximum",
        ],
        timeout=10,
    )
    if ps_ram.returncode == 0 and ps_ram.stdout.strip():
        try:
            spd = int(float(ps_ram.stdout.strip()))
            if spd > 0:
                ram_speed_mt_s = spd
        except ValueError:
            pass

    return {
        "cpu_model": cpu_model,
        "cpu_topology_note": None,
        "ram_speed_mt_s": ram_speed_mt_s,
        "gpu_model": gpu_model,
        "gpu_vram_gb": gpu_vram_gb,
        "gpu_memory_type": "Discrete GDDR" if ("nvidia" in gpu_model.lower() or "rx " in gpu_model.lower() or "arc " in gpu_model.lower()) else "System / Shared",
        "gpu_api_primary": f"CUDA / Vulkan / DX12 (Driver {driver_ver})" if driver_ver else "Vulkan / DX12",
        "ssd_model": ssd_model,
        "estimated_peak_watts": 220,
        "idle_wall_watts": 55.0,
        "price_usd": 1000,
        "price_note": "Desktop PC (override in Web UI or rig-config.json)",
        "workload_wall_watts": {
            "CPU": 140.0,
            "GPU Graphics": 220.0,
            "GPU Math": 200.0,
            "SSD and I/O Compute": 110.0,
        },
    }


def _linux_sysinfo() -> dict[str, Any]:
    cpu_model = platform.processor() or "x86_64 CPU"
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lower().startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break

    gpu_model = "Integrated / Unknown GPU"
    gpu_vram_gb: float | None = None
    driver_ver: str | None = None
    nv = shutil.which("nvidia-smi")
    if nv:
        res = run_cmd([nv, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"], timeout=8)
        if res.returncode == 0 and res.stdout.strip():
            parts = [p.strip() for p in res.stdout.splitlines()[0].split(",")]
            if len(parts) >= 3:
                gpu_model = parts[0]
                try:
                    gpu_vram_gb = round(float(parts[1]) / 1024.0, 2)
                except ValueError:
                    pass
                driver_ver = parts[2]

    return {
        "cpu_model": cpu_model,
        "cpu_topology_note": None,
        "gpu_model": gpu_model,
        "gpu_vram_gb": gpu_vram_gb,
        "gpu_memory_type": "Discrete GDDR" if nv else "System / Shared",
        "gpu_api_primary": f"CUDA / Vulkan (Driver {driver_ver})" if driver_ver else "Vulkan / OpenCL",
        "ssd_model": "NVMe / System Storage",
        "estimated_peak_watts": 200,
        "idle_wall_watts": 50.0,
        "price_usd": 1000,
        "price_note": "Linux Workstation (override in Web UI or rig-config.json)",
        "workload_wall_watts": {
            "CPU": 140.0,
            "GPU Graphics": 200.0,
            "GPU Math": 190.0,
            "SSD and I/O Compute": 100.0,
        },
    }


def collect_sysinfo(
    machine_id: str | None = None,
    scratch_target: Path | None = None,
    price_usd: int | None = None,
    idle_watts: float | None = None,
) -> dict[str, Any]:
    """Collect scrubbed, privacy-safe hardware and storage metadata."""
    target = scratch_target or SCRATCH_DIR
    target.mkdir(parents=True, exist_ok=True)
    disk = psutil.disk_usage(str(target))
    vm = psutil.virtual_memory()

    os_specific = _macos_sysinfo() if is_macos() else (_windows_sysinfo() if is_windows() else _linux_sysinfo())
    cfg = _load_rig_config_overrides()
    for key in ("price_usd", "price_note", "idle_wall_watts", "estimated_peak_watts", "workload_wall_watts", "display_name", "short_name"):
        if key in cfg and cfg[key] is not None:
            os_specific[key] = cfg[key]
    if price_usd is not None:
        os_specific["price_usd"] = int(price_usd)
        os_specific["price_note"] = "User-specified hardware cost"
    if idle_watts is not None:
        os_specific["idle_wall_watts"] = float(idle_watts)

    resolved_id = machine_id or cfg.get("machine_id") or _default_machine_id(
        os_specific.get("cpu_model", ""), os_specific.get("gpu_model", "")
    )

    label_map = {
        "m4-mac-mini": "M4 Mac Mini (10c CPU / 10c GPU / 16GB)",
        "zen2-1660s-pc": "Desktop PC (Zen 2 / GTX 1660 Super)",
    }

    display_name = os_specific.pop("display_name", None) or label_map.get(resolved_id) or f"{os_specific.get('cpu_model', resolved_id)}"

    return {
        "machine_id": resolved_id,
        "display_name": display_name,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "os": platform.system(),
        "os_release": platform.mac_ver()[0] if is_macos() else platform.version(),
        "arch": platform.machine(),
        "cpu_physical_cores": psutil.cpu_count(logical=False),
        "cpu_logical_cores": psutil.cpu_count(logical=True),
        "ram_total_gb": round(vm.total / (1024**3), 2),
        "ssd_total_gb": round(disk.total / (1024**3), 2),
        "ssd_free_gb_at_start": round(disk.free / (1024**3), 2),
        "ssd_used_percent_at_start": round(disk.percent, 1),
        **os_specific,
    }
