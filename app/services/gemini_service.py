"""All Gemini calls live here -- server-side only, API key never reaches
the client. Every call goes through resilience.call_with_resilience for
timeout/retry/circuit-breaking.
"""
import json
import logging
import os
import re

from flask import current_app

from app.services.resilience import call_with_resilience

logger = logging.getLogger("mathverse.gemini")

_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts")


def _load_prompt(name: str) -> str:
    with open(os.path.join(_PROMPTS_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


def _client():
    from google import genai

    api_key = current_app.config["GEMINI_API_KEY"]
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    return genai.Client(api_key=api_key)


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:python|json)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def _generate_text(system_prompt: str, user_content, job_id=None) -> str:
    model = current_app.config["GEMINI_MODEL"]

    def _call():
        client = _client()
        response = client.models.generate_content(
            model=model,
            contents=user_content,
            config={"system_instruction": system_prompt, "temperature": 0.4},
        )
        return response.text

    return call_with_resilience(
        _call, service="gemini", max_attempts=3, base_delay=2.0, job_id=job_id
    )


def generate_manim_script(prompt: str, job_id=None) -> str:
    """Turn a math question/concept into a runnable MathVerseScene script."""
    system_prompt = _load_prompt("manim_system_prompt.txt")
    raw = _generate_text(system_prompt, prompt, job_id=job_id)
    return _strip_code_fences(raw)


TIMEOUT_REPAIR_PROMPT = """The following Manim script did not crash -- it was still rendering when the
{timeout}s time limit expired. There is no traceback to fix: the animation is
simply too expensive to render in budget.

Return ONLY a corrected complete Python file - no fences, no explanation.
Make it substantially CHEAPER to render while still teaching the same idea:
- Cut to at most 6 next_section beats, merging or dropping the least essential.
- Shorten every run_time (0.3-0.8s) and every self.wait (<= 0.5s).
- Prefer Write/FadeIn/Create over Transform chains and per-character animation.
- Drop decorative mobjects; keep the diagram, the key steps, and the result.
- Keep the class name MathVerseScene and the same 4-region layout.

SCRIPT:
{script}
"""


def repair_manim_script(
    script: str, traceback_text: str, job_id=None, timed_out: bool = False
) -> str:
    """Ask Gemini to fix a script that failed to render.

    A timeout gets its own prompt: the standard repair prompt says not to
    simplify unless the error requires it, which is exactly backwards when
    the only problem is that the animation is too slow.
    """
    if timed_out:
        from flask import current_app

        user_content = TIMEOUT_REPAIR_PROMPT.format(
            script=script, timeout=current_app.config["MAX_RENDER_SECONDS"]
        )
        raw = _generate_text(
            "You are an expert Manim Community Edition developer.", user_content, job_id=job_id
        )
        return _strip_code_fences(raw)

    template = _load_prompt("repair_system_prompt.txt")
    user_content = template.format(script=script, traceback=traceback_text)
    # The repair prompt IS the full instruction; no separate system prompt needed.
    raw = _generate_text(
        "You are an expert Manim Community Edition developer.", user_content, job_id=job_id
    )
    return _strip_code_fences(raw)


def generate_narration(script: str, job_id=None) -> list[dict]:
    """Return [{"section": name, "text": ...}, ...] for each next_section beat."""
    system_prompt = _load_prompt("narration_system_prompt.txt")
    raw = _generate_text(system_prompt, script, job_id=job_id)
    cleaned = _strip_code_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"narration response was not valid JSON: {exc}\nRaw: {cleaned[:500]}") from exc
    if not isinstance(data, list):
        raise ValueError("narration response must be a JSON array")
    return data


def generate_chat_answer(prompt: str, job_id=None) -> str:
    """The fast, synchronous answer shown in chat before (and independently
    of) any video render. Handles both 'solve this' and 'explain this'
    prompts, since students ask both."""
    system_prompt = (
        "You are a friendly, precise math tutor answering in a chat window.\n"
        "- If the user asks you to SOLVE something, work through it in short "
        "numbered steps and end with a clearly labeled final answer.\n"
        "- If the user asks you to EXPLAIN a concept, give the intuition first, "
        "then the formal statement, then one short worked example.\n"
        "- Use $...$ for inline math and $$...$$ for display equations. Never "
        "use LaTeX environments like \\begin{align} or \\boxed.\n"
        "- Keep it under about 250 words. Be correct above all: never invent a "
        "formula or a numeric result.\n"
        "- Use plain prose and short paragraphs. No markdown headings."
    )
    return _generate_text(system_prompt, prompt, job_id=job_id)


def generate_text_solution(prompt: str, job_id=None) -> str:
    """Plain-text, KaTeX-friendly step-by-step solution -- used as the
    fallback when video rendering can't be completed."""
    system_prompt = (
        "You are a math tutor. Solve the following step by step. "
        "Use $...$ for inline LaTeX and $$...$$ for display equations. "
        "Number each step. End with a clearly labeled final answer."
    )
    return _generate_text(system_prompt, prompt, job_id=job_id)


def ask_whiteboard(
    mode: str, image_bytes: bytes, question_text: str | None = None, job_id=None
) -> str:
    """mode in {ask, explain_selection, check_work, solve_on_board}."""
    system_prompt = _load_prompt("whiteboard_system_prompt.txt")
    model = current_app.config["GEMINI_MODEL"]

    def _call():
        from google.genai import types

        client = _client()
        parts = [types.Part.from_bytes(data=image_bytes, mime_type="image/png")]
        text_prompt = f"mode: {mode}\n" + (question_text or "")
        parts.append(types.Part.from_text(text=text_prompt))
        response = client.models.generate_content(
            model=model,
            contents=parts,
            config={"system_instruction": system_prompt, "temperature": 0.3},
        )
        return response.text

    raw = call_with_resilience(
        _call, service="gemini", max_attempts=3, base_delay=2.0, job_id=job_id
    )
    return _strip_code_fences(raw)
