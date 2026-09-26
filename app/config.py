"""Configuration classes for MathVerse.

Config is selected via FLASK_ENV / the `config_name` passed to create_app().
ProdConfig fails fast at import time if required secrets are missing.
"""
import os

from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

# The `flask` CLI auto-loads .env, but plain entry points (`python wsgi.py`,
# `python celery_worker.py`, gunicorn) don't -- load it explicitly here so
# every way of starting the app agrees on the same config, and in
# particular the same database file.
load_dotenv(os.path.join(basedir, ".env"))


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class BaseConfig:
    # `or` (not .get's default arg) because .env ships SECRET_KEY= blank --
    # present-but-empty still needs to fall back to the dev default.
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-insecure-secret-key-change-me"
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(basedir, 'mathverse.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")

    ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
    ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")

    # Used when ElevenLabs is missing, invalid, or out of quota.
    # "edge" = free Microsoft neural voices via edge-tts (no API key).
    # "none" = no fallback; failures produce a silent video.
    TTS_FALLBACK_PROVIDER = os.environ.get("TTS_FALLBACK_PROVIDER", "edge")
    TTS_FALLBACK_VOICE = os.environ.get("TTS_FALLBACK_VOICE", "en-US-AriaNeural")

    MEDIA_ROOT = os.environ.get("MEDIA_ROOT", os.path.join(basedir, "media"))
    MANIM_QUALITY = os.environ.get("MANIM_QUALITY", "medium_quality")
    MAX_RENDER_SECONDS = int(os.environ.get("MAX_RENDER_SECONDS", "180"))
    MAX_REPAIR_ATTEMPTS = int(os.environ.get("MAX_REPAIR_ATTEMPTS", "3"))

    GUEST_DAILY_VIDEOS = int(os.environ.get("GUEST_DAILY_VIDEOS", "5"))
    USER_DAILY_VIDEOS = int(os.environ.get("USER_DAILY_VIDEOS", "25"))
    GUEST_DAILY_WHITEBOARD = int(os.environ.get("GUEST_DAILY_WHITEBOARD", "40"))
    USER_DAILY_WHITEBOARD = int(os.environ.get("USER_DAILY_WHITEBOARD", "200"))

    GUEST_TOKEN_COOKIE = "mv_guest_token"
    GUEST_TOKEN_MAX_AGE_DAYS = 30

    WTF_CSRF_ENABLED = True

    TESTING = False
    DEBUG = False


class DevConfig(BaseConfig):
    DEBUG = True
    WTF_CSRF_ENABLED = False


class TestConfig(BaseConfig):
    TESTING = True
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "test-secret-key"
    MEDIA_ROOT = os.path.join(basedir, "tests", "_media")


class ProdConfig(BaseConfig):
    DEBUG = False

    def __init__(self):
        required = [
            "SECRET_KEY",
            "DATABASE_URL",
            "GEMINI_API_KEY",
            "ELEVENLABS_API_KEY",
        ]
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables for production: {', '.join(missing)}"
            )

        # This build renders Manim in-process (see the security note at the
        # top of app/services/manim_service.py): model-generated Python runs
        # with only the AST allowlist protecting the host. Refuse to start in
        # production rather than let that reach real users silently.
        raise RuntimeError(
            "This build is local-only: it renders model-generated code without "
            "sandbox isolation. Restore containerized rendering (no network, "
            "read-only root, non-root user, resource limits) before serving "
            "other people's prompts."
        )


CONFIG_MAP = {
    "development": DevConfig,
    "testing": TestConfig,
    "production": ProdConfig,
    "default": DevConfig,
}


def get_config(name: str | None = None):
    name = name or os.environ.get("FLASK_ENV", "development")
    cls = CONFIG_MAP.get(name, DevConfig)
    if name == "production":
        return cls()  # triggers fail-fast validation
    return cls
