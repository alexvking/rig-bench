from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .common import (
    SCRATCH_DIR,
    find_binary,
    get_external_cache_dir,
    get_uv_env,
    is_macos,
    is_windows,
    run_cmd,
)

MODEL_SPECS = [
    {
        "tier_key": "tier1_7b_q4km",
        "label": "Qwen2.5-7B Q4_K_M (~4.7 GB, fits 6GB VRAM)",
        "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
        "hf_urls": [
            "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf"
        ],
    },
    {
        "tier_key": "tier2_14b_q4km",
        "label": "Qwen2.5-14B Q4_K_M (~9.0 GB, tests >6GB UMA vs PCIe spill)",
        "filename": "qwen2.5-14b-instruct-q4_k_m.gguf",
        "hf_urls": [
            "https://huggingface.co/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf"
        ],
    },
]

PYTORCH_WORKER_CODE = r"""
import json
import time
import torch

if torch.cuda.is_available():
    device = torch.device("cuda")
    backend = f"cuda ({torch.cuda.get_device_name(0)})"
    sync = torch.cuda.synchronize
elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
    device = torch.device("mps")
    backend = "mps (Apple Metal)"
    sync = torch.mps.synchronize
else:
    print(json.dumps({"status": "skipped", "reason": "Neither CUDA nor Metal MPS available in torch"}))
    raise SystemExit(0)

out = {"status": "ok", "backend": backend}

# 1. FP32 Dense GEMM (4096 x 4096)
n32 = 4096
a32 = torch.randn((n32, n32), device=device, dtype=torch.float32)
b32 = torch.randn((n32, n32), device=device, dtype=torch.float32)
for _ in range(3):
    _ = torch.matmul(a32, b32)
sync()
iters32 = 12
t0 = time.perf_counter()
for _ in range(iters32):
    _ = torch.matmul(a32, b32)
sync()
dt32 = time.perf_counter() - t0
out["fp32_gemm_4096_tflops"] = round((2.0 * (n32 ** 3) * iters32 / dt32) / 1e12, 3)
del a32, b32

# 2. FP16 Dense GEMM (8192 x 8192)
n16 = 8192
a16 = torch.randn((n16, n16), device=device, dtype=torch.float16)
b16 = torch.randn((n16, n16), device=device, dtype=torch.float16)
for _ in range(3):
    _ = torch.matmul(a16, b16)
sync()
iters16 = 10
t1 = time.perf_counter()
for _ in range(iters16):
    _ = torch.matmul(a16, b16)
sync()
dt16 = time.perf_counter() - t1
out["fp16_gemm_8192_tflops"] = round((2.0 * (n16 ** 3) * iters16 / dt16) / 1e12, 3)
del a16, b16

# 3. 2D Complex FFT (4096 x 4096)
try:
    nfft = 4096
    xfft = torch.randn((nfft, nfft), device=device, dtype=torch.complex64)
    for _ in range(2):
        _ = torch.fft.fft2(xfft)
    sync()
    iters_fft = 10
    t2 = time.perf_counter()
    for _ in range(iters_fft):
        _ = torch.fft.fft2(xfft)
    sync()
    dt_fft = time.perf_counter() - t2
    # 5 * N^2 * log2(N^2) FLOPs approximation for complex 2D FFT
    import math
    flops_fft = 5.0 * (nfft * nfft) * math.log2(nfft * nfft)
    out["fft2d_4096_gflops"] = round((flops_fft * iters_fft / dt_fft) / 1e9, 2)
    del xfft
except Exception as exc:
    out["fft2d_4096_gflops"] = None
    out["fft_note"] = str(exc)

# 4. Streaming Vector Triad Bandwidth (3 x 256 MB = 768 MB moved per iter, preallocated out=)
elems = 64 * 1024 * 1024  # 64M float32 = 256 MB per tensor
va = torch.ones(elems, device=device, dtype=torch.float32)
vb = torch.full((elems,), 2.0, device=device, dtype=torch.float32)
vc = torch.empty_like(va)
for _ in range(4):
    torch.add(va, vb, alpha=1.5, out=vc)
sync()
iters_bw = 25
t3 = time.perf_counter()
for _ in range(iters_bw):
    torch.add(va, vb, alpha=1.5, out=vc)
sync()
dt_bw = time.perf_counter() - t3
bytes_moved = 3.0 * elems * 4.0 * iters_bw
out["memory_triad_gb_s"] = round((bytes_moved / dt_bw) / 1e9, 2)

print("RIG_BENCH_TORCH_JSON:" + json.dumps(out))
"""

BLENDER_CYCLES_SCRIPT = r"""
import bpy
import json
import math
import sys
import time

target_backend = sys.argv[-2]  # METAL or CUDA
out_file = sys.argv[-1]

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.render.resolution_x = 1920
scene.render.resolution_y = 1080
scene.render.resolution_percentage = 100
scene.cycles.samples = 128
scene.cycles.use_denoising = False

prefs = bpy.context.preferences
cprefs = prefs.addons['cycles'].preferences
cprefs.compute_device_type = target_backend
cprefs.get_devices()

enabled_gpus = []
for dev in cprefs.devices:
    if dev.type != 'CPU':
        dev.use = True
        enabled_gpus.append(dev.name)
    else:
        dev.use = False

# Enable MetalRT on Apple Silicon if exposed
if hasattr(cprefs, 'metal_rt'):
    try:
        cprefs.metal_rt = 'ON'
    except Exception:
        pass

scene.cycles.device = 'GPU'

# Build glass + metallic Cornell-style path-tracing stress scene
glass_mat = bpy.data.materials.new(name="GlassDisp")
glass_mat.use_nodes = True
bsdf = glass_mat.node_tree.nodes.get("Principled BSDF")
if bsdf:
    if "Transmission Weight" in bsdf.inputs:
        bsdf.inputs["Transmission Weight"].default_value = 1.0
    elif "Transmission" in bsdf.inputs:
        bsdf.inputs["Transmission"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.04
    bsdf.inputs["IOR"].default_value = 1.52

gold_mat = bpy.data.materials.new(name="GoldMirror")
gold_mat.use_nodes = True
gbsdf = gold_mat.node_tree.nodes.get("Principled BSDF")
if gbsdf:
    gbsdf.inputs["Base Color"].default_value = (0.95, 0.72, 0.25, 1.0)
    gbsdf.inputs["Metallic"].default_value = 0.96
    gbsdf.inputs["Roughness"].default_value = 0.12

bpy.ops.mesh.primitive_plane_add(size=40.0, location=(0, 0, -2.2))

for i in range(48):
    ang = i * 0.52
    rad = 1.8 + (i % 6) * 1.35
    z = -1.2 + (i // 12) * 1.15
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=0.62, location=(math.cos(ang)*rad, math.sin(ang)*rad, z))
    obj = bpy.context.active_object
    obj.data.materials.append(glass_mat if (i % 2 == 0) else gold_mat)

for idx in range(3):
    ldata = bpy.data.lights.new(name=f"Area_{idx}", type='AREA')
    ldata.energy = 1400.0
    ldata.size = 4.0
    lobj = bpy.data.objects.new(name=f"Area_{idx}", object_data=ldata)
    a = idx * (2.0 * math.pi / 3.0)
    lobj.location = (math.cos(a) * 9.0, math.sin(a) * 9.0, 8.0)
    scene.collection.objects.link(lobj)

cam_data = bpy.data.cameras.new("Cam")
cam_obj = bpy.data.objects.new("Cam", cam_data)
scene.collection.objects.link(cam_obj)
scene.camera = cam_obj
cam_obj.location = (0.0, -14.5, 4.8)
cam_obj.rotation_euler = (1.28, 0.0, 0.0)

scene.render.filepath = out_file
t0 = time.perf_counter()
bpy.ops.render.render(write_still=False)
elapsed = time.perf_counter() - t0
samples_per_min = (128.0 / elapsed) * 60.0

print("RIG_BENCH_CYCLES_JSON:" + json.dumps({
    "backend": target_backend,
    "gpus": enabled_gpus,
    "samples": 128,
    "render_sec": round(elapsed, 2),
    "samples_per_min": round(samples_per_min, 1),
}))
"""


def run_pytorch_gpu_math() -> dict[str, Any]:
    uv_bin = shutil.which("uv") or str(Path.home() / "Library/Python/3.9/bin/uv")
    if not Path(uv_bin).exists() and not shutil.which("uv"):
        return {"status": "skipped", "reason": "uv binary not found for isolated PyTorch GPU worker"}

    with tempfile.TemporaryDirectory() as tmp:
        worker_script = Path(tmp) / "torch_worker.py"
        worker_script.write_text(PYTORCH_WORKER_CODE, encoding="utf-8")

        cmd = [uv_bin, "run", "--no-project", "--python", "3.12"]
        if is_windows() and shutil.which("nvidia-smi"):
            cmd.extend(["--index-url", "https://download.pytorch.org/whl/cu124"])
        cmd.extend(["--with", "torch", "python", str(worker_script)])

        proc = run_cmd(cmd, timeout=900, env=get_uv_env())
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        for line in combined.splitlines():
            if "RIG_BENCH_TORCH_JSON:" in line:
                return json.loads(line.split("RIG_BENCH_TORCH_JSON:", 1)[1].strip())
        return {
            "status": "error",
            "reason": "PyTorch worker did not return metrics",
            "tail": combined[-500:],
        }


def _parse_llama_bench_json(raw_stdout: str) -> dict[str, float | None]:
    brace = raw_stdout.find("[")
    if brace == -1:
        return {"pp512_tok_s": None, "tg128_tok_s": None}
    entries = json.loads(raw_stdout[brace:])
    pp_val = None
    tg_val = None
    for entry in entries:
        n_prompt = entry.get("n_prompt", 0)
        n_gen = entry.get("n_gen", 0)
        avg_ts = entry.get("avg_ts")
        if n_prompt > 0 and n_gen == 0 and avg_ts is not None:
            pp_val = round(float(avg_ts), 2)
        elif n_gen > 0 and n_prompt == 0 and avg_ts is not None:
            tg_val = round(float(avg_ts), 2)
    return {"pp512_tok_s": pp_val, "tg128_tok_s": tg_val}


def run_llama_bench_suite(quick: bool = False) -> dict[str, Any]:
    llama_bin = find_binary(
        ["llama-bench"],
        mac_candidates=["/opt/homebrew/bin/llama-bench", "/usr/local/bin/llama-bench"],
        win_candidates=[r"C:\Tools\llama.cpp\llama-bench.exe"],
    )
    if not llama_bin:
        hint = "brew install llama.cpp" if is_macos() else "uv run bench.py bootstrap"
        return {"status": "skipped", "reason": f"llama-bench not installed ({hint})"}

    models_dir = get_external_cache_dir() / "models"
    reps = "2" if quick else "3"
    tier_results: dict[str, Any] = {}

    specs = MODEL_SPECS[:1] if quick else MODEL_SPECS
    for spec in specs:
        model_path = models_dir / spec["filename"]
        if not model_path.is_file():
            tier_results[spec["tier_key"]] = {
                "status": "skipped",
                "label": spec["label"],
                "reason": f"Model not cached at {model_path} (run `uv run bench.py bootstrap --models`)",
            }
            continue

        # Attempt full GPU offload (-ngl 99); if 6 GB VRAM OOMs on Tier 2, fall back to partial offload (-ngl 22)
        for ngl in (99, 22):
            cmd = [
                llama_bin,
                "-m",
                str(model_path),
                "-p",
                "512",
                "-n",
                "128",
                "-r",
                reps,
                "-ngl",
                str(ngl),
                "-o",
                "json",
            ]
            proc = run_cmd(cmd, timeout=900, env=get_uv_env())
            if proc.returncode == 0 and "[" in (proc.stdout or ""):
                parsed = _parse_llama_bench_json(proc.stdout)
                tier_results[spec["tier_key"]] = {
                    "status": "ok",
                    "label": spec["label"],
                    "ngl_offload": ngl,
                    **parsed,
                }
                break
        else:
            tier_results[spec["tier_key"]] = {
                "status": "error",
                "label": spec["label"],
                "reason": "llama-bench failed at both -ngl 99 and -ngl 22",
            }

    return {"status": "ok", "models_cache_dir": str(models_dir), "tiers": tier_results}


def run_blender_cycles_gpu() -> dict[str, Any]:
    blender = find_binary(
        ["blender", "Blender"],
        mac_candidates=[
            "/Applications/Blender.app/Contents/MacOS/Blender",
            "~/Applications/Blender.app/Contents/MacOS/Blender",
        ],
        win_candidates=[
            r"C:\Program Files\Blender Foundation\Blender 5.3\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender\blender.exe",
        ],
    )
    if not blender:
        return {"status": "skipped", "reason": "Blender binary not found"}

    backend = "METAL" if is_macos() else "CUDA"
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(SCRATCH_DIR)) as tmp:
        script_path = Path(tmp) / "cycles_bench.py"
        script_path.write_text(BLENDER_CYCLES_SCRIPT, encoding="utf-8")
        out_img = str(Path(tmp) / "cycles_out.png")
        proc = run_cmd(
            [blender, "-b", "--factory-startup", "-P", str(script_path), "--", backend, out_img],
            timeout=900,
        )
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        for line in combined.splitlines():
            if "RIG_BENCH_CYCLES_JSON:" in line:
                payload = json.loads(line.split("RIG_BENCH_CYCLES_JSON:", 1)[1].strip())
                return {"status": "ok", **payload}
        return {"status": "error", "reason": "Failed to parse Blender Cycles JSON", "tail": combined[-500:]}


def run_blender_opendata_cli() -> dict[str, Any]:
    cli = find_binary(["benchmark-launcher-cli"])
    if not cli:
        return {"status": "skipped", "reason": "Blender OpenData benchmark-launcher-cli not installed (optional)"}

    device = "METAL" if is_macos() else "CUDA"
    proc = run_cmd([cli, "benchmark", "--device-type", device, "--json", "monster", "junkshop", "classroom"], timeout=1800)
    if proc.returncode != 0:
        return {"status": "error", "tail": ((proc.stdout or "") + (proc.stderr or ""))[-400:]}
    try:
        data = json.loads(proc.stdout)
        scenes = {}
        for item in data:
            sname = item.get("scene", {}).get("label", "scene")
            spm = item.get("stats", {}).get("samples_per_minute")
            if spm is not None:
                scenes[sname] = round(float(spm), 2)
        return {"status": "ok", "device": device, "scenes_samples_per_min": scenes}
    except Exception as exc:
        return {"status": "error", "reason": str(exc)}


def run_gpu_compute_suite(quick: bool = False) -> dict[str, Any]:
    return {
        "pytorch_parallel_math": run_pytorch_gpu_math(),
        "blender_cycles_gpu": run_blender_cycles_gpu(),
        "llama_bench": run_llama_bench_suite(quick=quick),
        "blender_opendata": run_blender_opendata_cli(),
    }
