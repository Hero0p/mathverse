import pytest

from app.services.script_validator import ScriptValidationError, validate_manim_script

VALID_SCRIPT = """
from manim import *
import numpy as np
import math

class MathVerseScene(Scene):
    def construct(self):
        self.next_section(name="intro")
        title = Text("Hello")
        self.play(Write(title))
        self.wait(1)
"""


def test_valid_script_passes():
    validate_manim_script(VALID_SCRIPT)  # should not raise


def test_missing_scene_class_rejected():
    script = "from manim import *\nclass NotTheRightScene(Scene):\n    def construct(self):\n        pass\n"
    with pytest.raises(ScriptValidationError):
        validate_manim_script(script)


def test_os_import_rejected():
    script = "import os\nfrom manim import *\nclass MathVerseScene(Scene):\n    def construct(self):\n        os.system('rm -rf /')\n"
    with pytest.raises(ScriptValidationError):
        validate_manim_script(script)


def test_os_system_call_rejected_even_without_direct_import():
    script = (
        "from manim import *\n"
        "class MathVerseScene(Scene):\n"
        "    def construct(self):\n"
        "        __import__('os').system('echo pwned')\n"
    )
    with pytest.raises(ScriptValidationError):
        validate_manim_script(script)


def test_subprocess_import_rejected():
    script = "import subprocess\nfrom manim import *\nclass MathVerseScene(Scene):\n    def construct(self):\n        pass\n"
    with pytest.raises(ScriptValidationError):
        validate_manim_script(script)


def test_open_call_rejected():
    script = (
        "from manim import *\n"
        "class MathVerseScene(Scene):\n"
        "    def construct(self):\n"
        "        f = open('/etc/passwd')\n"
    )
    with pytest.raises(ScriptValidationError):
        validate_manim_script(script)


def test_eval_and_exec_rejected():
    for call in ("eval('1+1')", "exec('print(1)')"):
        script = f"from manim import *\nclass MathVerseScene(Scene):\n    def construct(self):\n        {call}\n"
        with pytest.raises(ScriptValidationError):
            validate_manim_script(script)


def test_socket_and_network_libs_rejected():
    for module in ("socket", "requests", "urllib"):
        script = f"import {module}\nfrom manim import *\nclass MathVerseScene(Scene):\n    def construct(self):\n        pass\n"
        with pytest.raises(ScriptValidationError):
            validate_manim_script(script)


@pytest.mark.parametrize("path", [
    "/etc/passwd",
    "/usr/bin/python",
    "/home/user/.ssh/id_rsa",
    "C:\\Windows\\System32",
    "C:/Users/someone/secrets.txt",
    "\\\\fileserver\\share",
])
def test_filesystem_path_literal_rejected(path):
    script = (
        "from manim import *\n"
        "class MathVerseScene(Scene):\n"
        "    def construct(self):\n"
        f"        p = {path!r}\n"
    )
    with pytest.raises(ScriptValidationError):
        validate_manim_script(script)


@pytest.mark.parametrize("latex", [
    r"\\ \text{such that } DE = EF",   # LaTeX line break -- the false positive
    r"a^2 + b^2 = c^2",
    r"\frac{1}{2} \times b \times h",
    r"MN \parallel BC \\ MN = \frac{1}{2} BC",
    r"\begin{cases} x = 1 \\ y = 2 \end{cases}",
    "5/2",                              # a bare slash is division, not a path
    "1 / 2 = 0.5",
])
def test_latex_strings_are_not_mistaken_for_paths(latex):
    """Regression: `\\\\` is a LaTeX newline and appears in most multi-line
    MathTex. Treating it as a UNC path rejected valid math scripts."""
    script = (
        "from manim import *\n"
        "class MathVerseScene(Scene):\n"
        "    def construct(self):\n"
        "        self.next_section(name='intro')\n"
        f"        eq = MathTex({latex!r})\n"
        "        self.play(Write(eq))\n"
    )
    validate_manim_script(script)  # must not raise


def test_dunder_globals_access_rejected():
    script = (
        "from manim import *\n"
        "class MathVerseScene(Scene):\n"
        "    def construct(self):\n"
        "        x = (1).__class__.__globals__\n"
    )
    with pytest.raises(ScriptValidationError):
        validate_manim_script(script)


def test_syntax_error_rejected():
    with pytest.raises(ScriptValidationError):
        validate_manim_script("def broken(:\n")
