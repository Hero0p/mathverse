"""Runs generated Manim scripts and collects the per-section clips produced
by `--save_sections`.

SECURITY NOTE -- read before deploying this anywhere.

Manim runs directly in this process's environment. Generated scripts are
model-authored code, and the AST allowlist in script_validator.py (which
rejects imports and calls outside manim/numpy/math, plus filesystem and
dunder access) is the ONLY thing between a generated script and this
machine. That is defense in depth, not a sandbox.

This is a deliberate trade-off for a single-user local build: it removes the
Docker requirement entirely. Serving other people's prompts from this code
would need real isolation restored first -- a container with no network, a
read-only root, a non-root user, and memory/cpu/pid limits. ProdConfig
refuses to start for exactly this reason.
"""
import glob
import logging
import os
import re
import subprocess
import sys
import tempfile

from flask import current_app

from app.services.script_validator import ScriptValidationError, validate_manim_script  # noqa: F401

logger = logging.getLogger("mathverse.manim")

QUALITY_FLAGS = {
    "low_quality": "ql",
    "medium_quality": "qm",
    "high_quality": "qh",
    "production_quality": "qk",
}


class RenderError(RuntimeError):
    def __init__(self, message: str, traceback_text: str = "", timed_out: bool = False):
        super().__init__(message)
        self.traceback_text = traceback_text or message
        # A timeout needs a different repair strategy than a crash: there is
        # no traceback to fix, the script is simply too heavy to render in
        # budget, so the model must be told to cut scope rather than patch code.
        self.timed_out = timed_out


def _quality_letters() -> str:
    return QUALITY_FLAGS.get(current_app.config["MANIM_QUALITY"], "qh")


def _collect_section_clips(search_root: str) -> list[str]:
    """Find the per-section clips under `search_root`.

    Manim writes them to media/videos/<scene>/<quality>/sections/ and names
    them "<Scene>_<index>_<section name>.mp4", so a plain sort puts them in
    playback order via the zero-padded index.
    """
    return sorted(
        glob.glob(os.path.join(search_root, "**", "sections", "*.mp4"), recursive=True)
    )


def render_scene(script: str, job_id, work_dir: str | None = None) -> list[str]:
    """Validate and render `script`, returning per-section clip paths in order.

    Raises ScriptValidationError *before executing anything* if the script
    contains constructs outside the AST allowlist, and RenderError (carrying
    the captured traceback, for the self-repair loop) if rendering fails.
    """
    validate_manim_script(script)  # raises ScriptValidationError, never suppressed

    work_dir = work_dir or tempfile.mkdtemp(prefix=f"mv-render-{job_id}-")
    os.makedirs(work_dir, exist_ok=True)
    scene_path = os.path.join(work_dir, "scene.py")
    with open(scene_path, "w", encoding="utf-8") as f:
        f.write(script)

    clips = _render(scene_path, work_dir, job_id)
    if not clips:
        raise RenderError(
            "render succeeded but produced no section clips -- "
            "check that next_section() was called"
        )
    return clips


def _render(scene_path: str, work_dir: str, job_id) -> list[str]:
    timeout = current_app.config["MAX_RENDER_SECONDS"]
    media_dir = os.path.join(work_dir, "media")

    cmd = [
        sys.executable, "-m", "manim",
        f"-{_quality_letters()}",
        scene_path, "MathVerseScene",
        "--save_sections",
        "--media_dir", media_dir,
        "--disable_caching",
    ]

    logger.info("job=%s rendering: %s", job_id, " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=work_dir,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(
            f"render timed out after {timeout}s", str(exc), timed_out=True
        ) from exc
    except FileNotFoundError as exc:
        raise RenderError(
            "Manim isn't installed in this environment. "
            "Install it with: pip install -r requirements-render.txt",
            str(exc),
        ) from exc

    if result.returncode != 0:
        raise RenderError(
            "manim render failed",
            clean_render_output(result.stderr or result.stdout),
        )

    return _collect_section_clips(media_dir)


_PROGRESS_BAR_RE = re.compile(r"\d+%\|| it/s\]|\?it/s\]")


def clean_render_output(text: str, keep_chars: int = 6000) -> str:
    """Reduce Manim's stderr to something a human (or the repair prompt) can
    actually use.

    Manim renders each animation with a tqdm progress bar, which floods
    stderr with thousands of redraw lines. A real traceback appears at the
    very END of that flood, so we drop the progress noise and keep the tail
    rather than the head -- truncating from the front discards the only part
    that explains the failure.
    """
    if not text:
        return ""
    kept = [
        line for line in text.splitlines()
        if line.strip() and not _PROGRESS_BAR_RE.search(line)
    ]
    cleaned = "\n".join(kept).strip()
    if len(cleaned) > keep_chars:
        cleaned = "...(earlier output trimmed)...\n" + cleaned[-keep_chars:]
    return cleaned


_SECTION_NAME_RE = re.compile(r"^.*?_(?:\d+_)?(?P<name>.+)$")


def section_name_from_clip(clip_path: str) -> str:
    """Recover the beat name from a clip Manim named
    "<Scene>_<index>_<section name>.mp4"."""
    base = os.path.splitext(os.path.basename(clip_path))[0]
    match = _SECTION_NAME_RE.match(base)
    return match.group("name") if match else base
