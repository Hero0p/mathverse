#!/usr/bin/env python3
"""Start MathVerse with a single command:  python run.py

No Docker, no Redis, no second terminal. This script:

  1. loads .env (creating it from .env.example on a first run)
  2. applies database migrations
  3. seeds the Arena question bank if the database is empty
  4. reports what optional features are available (API keys, Manim, ffmpeg)
  5. serves the app -- video rendering runs on a background thread in-process

Flags:
  --port N     serve on a different port (default 5000)
  --debug      enable Flask's reloader and debug pages
"""
import argparse
import importlib.util
import os
import shutil
import socket
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- tiny console helpers -------------------------------------------------
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


# flush every line: startup progress is the point of this output, and it would
# otherwise sit in the buffer whenever stdout is piped to a file.
def say(text=""):  print(text, flush=True)
def ok(msg):       say(f"  {_c('32', '[ok]')}   {msg}")
def warn(msg):     say(f"  {_c('33', '[warn]')} {msg}")
def fail(msg):     say(f"  {_c('31', '[fail]')} {msg}")
def step(msg):     say(f"\n{_c('36;1', '>')} {_c('1', msg)}")


def die(msg, *hints):
    fail(msg)
    for hint in hints:
        say(f"         {hint}")
    sys.exit(1)


def load_environment():
    step("Environment")
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        example = BASE_DIR / ".env.example"
        if not example.exists():
            die(".env and .env.example are both missing.")
        shutil.copy(example, env_path)
        warn("created .env from .env.example -- add your GEMINI_API_KEY to it")

    from dotenv import load_dotenv

    load_dotenv(env_path)
    ok(f"loaded {env_path.name}")


def prepare_database():
    step("Database")
    from flask_migrate import upgrade as alembic_upgrade

    from app import create_app
    from app.models.question import Question

    app = create_app("development")
    with app.app_context():
        alembic_upgrade()  # idempotent: a no-op when already at head
        ok("migrations up to date")

        if Question.query.count() == 0:
            from scripts.seed_dev import run as seed

            seed()
        else:
            ok(f"{Question.query.count()} Arena questions loaded")
    return app


def preflight(app):
    step("Features")

    if os.environ.get("GEMINI_API_KEY"):
        ok("Gemini key found -- chat, whiteboard AI and video scripting enabled")
    else:
        warn("GEMINI_API_KEY is empty -- chat and whiteboard AI will not work")
        say("         get a free key at https://aistudio.google.com/apikey")

    with app.app_context():
        from app.services.tts_service import provider_chain

        chain = provider_chain()
    if chain:
        voice = app.config["TTS_FALLBACK_VOICE"]
        detail = f" (fallback voice: {voice})" if "edge" in chain else ""
        ok(f"narration via {' -> '.join(chain)}{detail}")
    else:
        warn("no TTS provider -- videos render silently (handled gracefully)")
        say("         set TTS_FALLBACK_PROVIDER=edge in .env for free narration")

    manim_ready = importlib.util.find_spec("manim") is not None
    latex_ready = shutil.which("latex") is not None
    ffmpeg_ready = shutil.which("ffmpeg") is not None

    if manim_ready and latex_ready and ffmpeg_ready:
        ok(f"video rendering ready (quality={app.config['MANIM_QUALITY']})")
    else:
        missing = []
        if not manim_ready:
            missing.append("manim (pip install -r requirements-render.txt)")
        if not latex_ready:
            missing.append("latex (install MiKTeX or TeX Live)")
        if not ffmpeg_ready:
            missing.append("ffmpeg (add it to PATH)")
        warn("video rendering unavailable -- missing: " + ", ".join(missing))
        say("         everything else still works; chat falls back to text answers")


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main():
    parser = argparse.ArgumentParser(description="Start MathVerse.")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true", help="Flask debug mode + reloader")
    args = parser.parse_args()

    say(_c("35;1", "\n  MathVerse"))
    say(_c("90", "  " + "-" * 9))

    load_environment()
    app = prepare_database()
    preflight(app)

    if not port_is_free(args.port):
        die(f"port {args.port} is already in use.",
            f"Close whatever is using it, or run: python run.py --port {args.port + 1}")

    say(f"\n{_c('32;1', '  Ready')} -> {_c('4', f'http://localhost:{args.port}')}")
    say(_c("90", "  Ctrl+C to stop\n"))

    from app.extensions import socketio
    from app.tasks import video_queue

    try:
        socketio.run(app, host="0.0.0.0", port=args.port,
                     debug=args.debug, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        pass
    finally:
        video_queue.shutdown()
        say("\n  stopped.\n")


if __name__ == "__main__":
    main()
