from __future__ import annotations

import json
import re
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from .common import SCRATCH_DIR, find_binary, is_macos, is_windows, run_cmd

BLENDER_EEVEE_SCRIPT = r"""
import bpy
import math
import time
import json
import sys

scene = bpy.context.scene
# Blender 4.2+ uses BLENDER_EEVEE_NEXT or BLENDER_EEVEE depending on version
available_engines = {e.identifier for e in scene.render.bl_rna.properties['engine'].enum_items}
scene.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in available_engines else 'BLENDER_EEVEE'
scene.render.resolution_x = 2560
scene.render.resolution_y = 1440
scene.render.resolution_percentage = 100
if hasattr(scene.eevee, 'taa_render_samples'):
    scene.eevee.taa_render_samples = 32

# Clear default objects
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in available_engines else 'BLENDER_EEVEE'
scene.render.resolution_x = 2560
scene.render.resolution_y = 1440
scene.render.resolution_percentage = 100

# Create metallic PBR material
mat = bpy.data.materials.new(name="BenchMetal")
mat.use_nodes = True
bsdf = mat.node_tree.nodes.get("Principled BSDF")
if bsdf:
    bsdf.inputs["Metallic"].default_value = 0.85
    bsdf.inputs["Roughness"].default_value = 0.22

# Base high-poly subdivided icosphere mesh shared across 400 instances
bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=5, radius=0.45)
base_obj = bpy.context.active_object
base_obj.data.materials.append(mat)
mesh_data = base_obj.data

for i in range(1, 400):
    angle = i * 0.37
    dist = 2.0 + (i % 25) * 0.55
    z = math.sin(i * 0.21) * 3.0
    obj = bpy.data.objects.new(f"Inst_{i}", mesh_data)
    obj.location = (math.cos(angle) * dist, math.sin(angle) * dist, z)
    scene.collection.objects.link(obj)

# Dynamic lights
for idx in range(6):
    light_data = bpy.data.lights.new(name=f"Light_{idx}", type='POINT')
    light_data.energy = 1800.0
    light_obj = bpy.data.objects.new(name=f"Light_{idx}", object_data=light_data)
    ang = idx * (math.pi / 3.0)
    light_obj.location = (math.cos(ang) * 10.0, math.sin(ang) * 10.0, 5.0)
    scene.collection.objects.link(light_obj)

cam_data = bpy.data.cameras.new("Camera")
cam_obj = bpy.data.objects.new("Camera", cam_data)
scene.collection.objects.link(cam_obj)
scene.camera = cam_obj
cam_obj.location = (0.0, -22.0, 7.5)
cam_obj.rotation_euler = (1.25, 0.0, 0.0)

out_dir = sys.argv[-1]
scene.render.image_settings.file_format = 'BMP'  # zero PNG CPU compression bottleneck

# Keyframe camera orbit across frames 1..25 so animation=True keeps GPU scene/shaders resident
frames = 24
scene.frame_start = 1
scene.frame_end = 1
scene.render.filepath = f"{out_dir}/warmup_"
bpy.ops.render.render(animation=True)

for f in range(2, 2 + frames):
    ang = (f - 2) * 0.12
    scene.frame_set(f)
    cam_obj.location = (math.sin(ang) * 22.0, -math.cos(ang) * 22.0, 7.5)
    cam_obj.rotation_euler = (1.25, 0.0, ang)
    cam_obj.keyframe_insert(data_path="location", frame=f)
    cam_obj.keyframe_insert(data_path="rotation_euler", frame=f)

scene.frame_start = 2
scene.frame_end = 1 + frames
scene.render.filepath = f"{out_dir}/anim_"
t0 = time.perf_counter()
bpy.ops.render.render(animation=True)
elapsed = time.perf_counter() - t0
print("RIG_BENCH_EEVEE_JSON:" + json.dumps({
    "engine": scene.render.engine,
    "frames": frames,
    "elapsed_sec": round(elapsed, 3),
    "fps_1440p": round(frames / elapsed, 2),
}))
"""


def _find_gravitymark() -> str | None:
    return find_binary(
        ["GravityMark"],
        mac_candidates=[
            "/Applications/GravityMark.app/Contents/MacOS/GravityMark",
            "~/Applications/GravityMark.app/Contents/MacOS/GravityMark",
        ],
        win_candidates=[
            r"C:\Program Files\GravityMark\bin\GravityMark.exe",
            r"%LOCALAPPDATA%\Programs\GravityMark\bin\GravityMark.exe",
        ],
    )


def _find_blender() -> str | None:
    return find_binary(
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


def run_gravitymark_cli(quick: bool = False) -> dict[str, Any]:
    binary = _find_gravitymark()
    if not binary:
        install_hint = (
            "Download GravityMark macOS DMG from tellusim.com"
            if is_macos()
            else "Download GravityMark Windows installer or winget install Tellusim.GravityMark"
        )
        return {"status": "skipped", "reason": f"GravityMark binary not found ({install_hint})"}

    apis = ["metal"] if is_macos() else (["vulkan", "dx12"] if not quick else ["vulkan"])
    api_results: dict[str, Any] = {}

    for api in apis:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / f"gravitymark_{api}.log"
            cmd = [
                binary,
                f"-api",
                api,
                "-width",
                "2560",
                "-height",
                "1440",
                "-fullscreen",
                "0",
                "-asteroids",
                "200000",
                "-benchmark",
                "1",
                "-close",
                "1",
                "-log",
                str(log_path),
            ]
            t0 = time.perf_counter()
            proc = run_cmd(cmd, timeout=600)
            elapsed = time.perf_counter() - t0
            combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
            if log_path.exists():
                combined += "\n" + log_path.read_text(encoding="utf-8", errors="replace")

            score_match = re.search(r"Score[:\s]+([0-9]+(?:\.[0-9]+)?)", combined, re.IGNORECASE)
            fps_match = re.search(r"FPS[:\s]+([0-9]+(?:\.[0-9]+)?)", combined, re.IGNORECASE)
            api_results[api] = {
                "status": "ok" if (score_match or fps_match) else "completed_unparsed",
                "score": float(score_match.group(1)) if score_match else None,
                "avg_fps_1440p": float(fps_match.group(1)) if fps_match else None,
                "elapsed_sec": round(elapsed, 2),
                "returncode": proc.returncode,
            }

    primary_api = apis[0]
    primary = api_results.get(primary_api, {})
    return {
        "status": primary.get("status", "error"),
        "primary_api": primary_api,
        "score_1440p": primary.get("score"),
        "avg_fps_1440p": primary.get("avg_fps_1440p"),
        "apis": api_results,
    }


def run_blender_eevee_raster() -> dict[str, Any]:
    blender = _find_blender()
    if not blender:
        return {"status": "skipped", "reason": "Blender binary not found"}

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(SCRATCH_DIR)) as tmp:
        script_path = Path(tmp) / "eevee_bench.py"
        script_path.write_text(BLENDER_EEVEE_SCRIPT, encoding="utf-8")
        # Note: EEVEE requires GPU windowing/Metal/OpenGL context; run without -b if needed or with -b on Metal/Vulkan
        proc = run_cmd([blender, "-b", "--factory-startup", "-P", str(script_path), "--", tmp], timeout=600)
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        for line in combined.splitlines():
            if "RIG_BENCH_EEVEE_JSON:" in line:
                payload = json.loads(line.split("RIG_BENCH_EEVEE_JSON:", 1)[1].strip())
                return {"status": "ok", **payload}
        return {
            "status": "error",
            "reason": "Failed to parse Blender EEVEE output",
            "tail": combined[-500:],
        }


def parse_3dmark_result_archive(archive_path: Path) -> dict[str, Any]:
    """Extract Steel Nomad / Steel Nomad Light / Wild Life Extreme scores from a .3dmark-result ZIP file."""
    if not archive_path.is_file():
        raise FileNotFoundError(f"3DMark result file not found: {archive_path}")

    extracted: dict[str, Any] = {"source_file": archive_path.name}
    with zipfile.ZipFile(archive_path, "r") as zf:
        names = zf.namelist()
        xml_candidates = [n for n in names if n.lower().endswith(".xml")]
        json_candidates = [n for n in names if n.lower().endswith(".json")]

        for jname in json_candidates:
            try:
                raw = json.loads(zf.read(jname).decode("utf-8", errors="replace"))
                if isinstance(raw, dict):
                    extracted.setdefault("json_keys", []).append(jname)
            except Exception:
                pass

        for xname in xml_candidates:
            xml_bytes = zf.read(xname)
            root = ET.fromstring(xml_bytes)
            for elem in root.iter():
                tag = elem.tag.split("}")[-1].lower()
                text = (elem.text or "").strip()
                if not text:
                    continue
                if any(k in tag for k in ("steelnomad", "score", "fps", "graphicsscore", "benchmarkname")):
                    try:
                        extracted[tag] = float(text)
                    except ValueError:
                        extracted[tag] = text
    return extracted


def run_gpu_raster_suite(quick: bool = False) -> dict[str, Any]:
    return {
        "gravitymark_1440p": run_gravitymark_cli(quick=quick),
        "blender_eevee_1440p": run_blender_eevee_raster(),
    }
