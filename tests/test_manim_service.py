"""Renderer behaviour.

The security-critical property here is that AST validation happens *before*
Manim is ever invoked. With containerized rendering removed, that allowlist
is the only thing protecting the host, so these tests matter more, not less.
"""
import subprocess

import pytest

from app.config import ProdConfig
from app.services import manim_service
from app.services.manim_service import RenderError
from app.services.script_validator import ScriptValidationError

VALID_SCRIPT = """from manim import *

class MathVerseScene(Scene):
    def construct(self):
        self.next_section(name="intro")
        self.wait(1)
"""

MALICIOUS_SCRIPT = """from manim import *
import os

class MathVerseScene(Scene):
    def construct(self):
        os.system("echo pwned")
"""


@pytest.fixture()
def never_runs(monkeypatch):
    """Fail loudly if anything actually spawns a render subprocess."""
    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run was called -- script was executed!")

    monkeypatch.setattr(subprocess, "run", _boom)


def test_malicious_script_rejected_before_manim_is_invoked(app, never_runs, tmp_path):
    """The whole point of the allowlist: `import os` never reaches execution."""
    with pytest.raises(ScriptValidationError):
        manim_service.render_scene(MALICIOUS_SCRIPT, job_id=1, work_dir=str(tmp_path))


@pytest.mark.parametrize("snippet", [
    "import subprocess",
    "f = open('/etc/passwd')",
    "eval('1+1')",
    "x = (1).__class__.__globals__",
])
def test_other_escapes_also_rejected_before_execution(app, never_runs, tmp_path, snippet):
    script = (
        "from manim import *\n\n"
        "class MathVerseScene(Scene):\n"
        "    def construct(self):\n"
        f"        {snippet}\n"
    )
    with pytest.raises(ScriptValidationError):
        manim_service.render_scene(script, job_id=1, work_dir=str(tmp_path))


def test_no_section_clips_raises_render_error(app, monkeypatch, tmp_path):
    monkeypatch.setattr(manim_service, "_render", lambda *a, **k: [])
    with pytest.raises(RenderError, match="no section clips"):
        manim_service.render_scene(VALID_SCRIPT, job_id=1, work_dir=str(tmp_path))


def test_timeout_is_flagged_distinctly_from_a_crash(app, monkeypatch, tmp_path):
    """A timeout has no traceback to patch, so it must be routed to the
    'make it cheaper' repair prompt rather than the 'fix the bug' one."""
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="manim", timeout=180)

    monkeypatch.setattr(subprocess, "run", _timeout)
    with pytest.raises(RenderError) as excinfo:
        manim_service.render_scene(VALID_SCRIPT, job_id=1, work_dir=str(tmp_path))
    assert excinfo.value.timed_out is True


def test_crash_is_not_flagged_as_timeout(app, monkeypatch, tmp_path):
    class _Result:
        returncode = 1
        stderr = "AttributeError: 'Text' object has no attribute 'set_max_width'"
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(RenderError) as excinfo:
        manim_service.render_scene(VALID_SCRIPT, job_id=1, work_dir=str(tmp_path))
    assert excinfo.value.timed_out is False
    assert "set_max_width" in excinfo.value.traceback_text


def test_missing_manim_gives_an_actionable_message(app, monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no manim")),
    )
    with pytest.raises(RenderError, match="requirements-render.txt"):
        manim_service.render_scene(VALID_SCRIPT, job_id=1, work_dir=str(tmp_path))


def test_timeout_repair_asks_the_model_to_cut_scope(app, monkeypatch):
    from app.services import gemini_service

    captured = {}
    monkeypatch.setattr(
        gemini_service, "_generate_text",
        lambda sp, uc, job_id=None: captured.update(content=uc) or "from manim import *",
    )
    with app.test_request_context():
        gemini_service.repair_manim_script("script body", "", timed_out=True)

    assert "time limit expired" in captured["content"]
    assert "at most 6 next_section beats" in captured["content"]
    assert "Do not simplify" not in captured["content"]


def test_crash_repair_uses_the_traceback_prompt(app, monkeypatch):
    from app.services import gemini_service

    captured = {}
    monkeypatch.setattr(
        gemini_service, "_generate_text",
        lambda sp, uc, job_id=None: captured.update(content=uc) or "from manim import *",
    )
    with app.test_request_context():
        gemini_service.repair_manim_script("script body", "NameError: boom", timed_out=False)

    assert "NameError: boom" in captured["content"]
    assert "Do not simplify" in captured["content"]


@pytest.mark.parametrize("quality,expected", [
    ("low_quality", "ql"), ("medium_quality", "qm"),
    ("high_quality", "qh"), ("production_quality", "qk"),
    ("something_odd", "qh"),
])
def test_quality_letters_carry_no_leading_dash(app, quality, expected):
    app.config["MANIM_QUALITY"] = quality
    with app.test_request_context():
        assert manim_service._quality_letters() == expected


@pytest.mark.parametrize("filename,expected", [
    ("MathVerseScene_0000_intro.mp4", "intro"),
    ("MathVerseScene_0012_step_two.mp4", "step_two"),
    ("MathVerseScene_conclusion.mp4", "conclusion"),
])
def test_section_name_recovered_from_clip(filename, expected):
    assert manim_service.section_name_from_clip(filename) == expected


def test_progress_bars_are_stripped_and_the_traceback_survives():
    """Manim floods stderr with tqdm redraws and puts the real traceback at
    the very end. Truncating from the front kept only the noise -- and fed
    that noise to the repair prompt, which then had nothing to fix."""
    noise = "\n".join(
        f"Animation {i}: FadeIn(Text('x')):  73%|#######2  | 8/11 [00:00<00:00, 76.18it/s]"
        for i in range(400)
    )
    traceback = (
        "  File \"scene.py\", line 246, in construct\n"
        "    final_c_val_calc = calc_step3.submobjects[1]\n"
        "IndexError: list index out of range"
    )
    cleaned = manim_service.clean_render_output(noise + "\n" + traceback)

    assert "IndexError: list index out of range" in cleaned
    assert "submobjects[1]" in cleaned
    assert "it/s]" not in cleaned
    assert len(cleaned) < len(noise)


def test_clean_render_output_handles_empty_input():
    assert manim_service.clean_render_output("") == ""
    assert manim_service.clean_render_output(None) == ""


def test_render_failure_stores_the_cleaned_traceback(app, monkeypatch, tmp_path):
    class _Result:
        returncode = 1
        stdout = ""
        stderr = (
            "Animation 0: FadeIn(Text('x')):  50%|#####     | 4/8 [00:00<00:00, 36.20it/s]\n"
            "IndexError: list index out of range"
        )

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(RenderError) as excinfo:
        manim_service.render_scene(VALID_SCRIPT, job_id=1, work_dir=str(tmp_path))

    assert "IndexError" in excinfo.value.traceback_text
    assert "it/s]" not in excinfo.value.traceback_text


def test_production_is_refused_because_rendering_is_unsandboxed(monkeypatch):
    """This build runs model-generated code in-process. Production must not
    start rather than silently exposing that to real users."""
    for key, value in {
        "SECRET_KEY": "x", "DATABASE_URL": "postgresql://x/y",
        "GEMINI_API_KEY": "x", "ELEVENLABS_API_KEY": "x",
    }.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match="local-only"):
        ProdConfig()
