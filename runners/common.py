from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

IS_FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _resolve_writable_root() -> Path:
    if not IS_FROZEN:
        return Path(__file__).resolve().parent.parent
    exe_dir = Path(sys.executable).resolve().parent
    candidate = exe_dir / "rig-bench-data"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return candidate
    except Exception:
        fallback = Path.home() / "rig-bench-data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


REPO_ROOT = _resolve_writable_root()
RESULTS_DIR = REPO_ROOT / "results" / "runs"
DOCS_DIR = REPO_ROOT / "docs"
DOCS_DATA_DIR = DOCS_DIR / "data"
SCRATCH_DIR = REPO_ROOT / ".scratch"


def ensure_frozen_docs() -> None:
    """Copy bundled Web UI assets into the writable data directory when running as a frozen .exe."""
    if not IS_FROZEN:
        return
    src_docs = BUNDLE_ROOT / "docs"
    if not src_docs.is_dir():
        return
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    for rel in ("index.html", "app.js", "styles.css"):
        src = src_docs / rel
        if src.is_file():
            shutil.copy2(src, DOCS_DIR / rel)
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_windows() -> bool:
    return platform.system() == "Windows"


def get_external_cache_dir() -> Path:
    """Prefer /Volumes/1T_SSD/.rig-bench-cache on the Mac Mini to preserve internal 256 GB SSD headroom."""
    env_override = os.environ.get("RIG_BENCH_CACHE_DIR")
    if env_override:
        path = Path(env_override).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    if is_macos():
        ext_ssd = Path("/Volumes/1T_SSD")
        if ext_ssd.exists() and os.access(ext_ssd, os.W_OK):
            cache = ext_ssd / ".rig-bench-cache"
            cache.mkdir(parents=True, exist_ok=True)
            return cache

    local_cache = REPO_ROOT / ".cache"
    local_cache.mkdir(parents=True, exist_ok=True)
    return local_cache


def get_uv_env() -> dict[str, str]:
    """Return environment dict routing large model caches to external SSD while keeping uv lockfiles on APFS/NTFS."""
    env = os.environ.copy()
    cache_root = get_external_cache_dir()
    # Note: /Volumes/1T_SSD may be exFAT (no POSIX flock for uv cache), whereas APFS uses zero-copy clonefile()
    env.setdefault("HF_HOME", str(cache_root / "huggingface"))
    return env


def find_binary(
    names: Sequence[str],
    mac_candidates: Sequence[str | Path] = (),
    win_candidates: Sequence[str | Path] = (),
) -> str | None:
    """Locate a CLI executable on PATH or known OS-specific install paths."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found

    candidates = mac_candidates if is_macos() else (win_candidates if is_windows() else ())
    for candidate in candidates:
        expanded = Path(os.path.expandvars(str(candidate))).expanduser()
        if expanded.is_file():
            return str(expanded)

    # Check tool cache directory
    tools_dir = get_external_cache_dir() / "tools"
    if tools_dir.exists():
        for name in names:
            suffix = ".exe" if is_windows() and not name.endswith(".exe") else ""
            for match in tools_dir.rglob(f"{name}{suffix}"):
                if match.is_file():
                    return str(match)

    return None


def win_subprocess_kwargs() -> dict[str, Any]:
    """Suppress console window popups when spawning child processes from a Windows GUI app."""
    if is_windows() and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def run_cmd(
    cmd: Sequence[str],
    *,
    timeout: int = 1800,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run subprocess safely with UTF-8 decoding and captured output."""
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
        **win_subprocess_kwargs(),
    )
