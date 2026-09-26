"""Text-to-speech with automatic fallback.

Providers are tried in order until one produces audio:

  1. elevenlabs -- best quality, needs a paid API key
  2. the provider named by TTS_FALLBACK_PROVIDER (default "edge")

If every provider fails, TTSUnavailableError is raised and the caller ships
a silent video with on-screen text (spec 12: graceful degradation), so a
missing or expired key degrades the result rather than failing the job.

Fallback providers:

  edge  - Microsoft Edge's neural voices via the `edge-tts` client. Free, no
          API key, good quality. It is a cloud service, not a local model, so
          it needs an internet connection.
  none  - disable fallback; ElevenLabs failures go straight to a silent video.

Set the voice with TTS_FALLBACK_VOICE (e.g. en-US-AriaNeural,
en-GB-RyanNeural, en-US-GuyNeural). Run `edge-tts --list-voices` to see them.
"""
import asyncio
import logging
import os

import requests
from flask import current_app

from app.services.resilience import CircuitOpenError, call_with_resilience

logger = logging.getLogger("mathverse.tts")

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
EDGE_TIMEOUT_SECONDS = 60


class TTSUnavailableError(RuntimeError):
    """No configured provider could produce audio."""


class ProviderUnavailable(RuntimeError):
    """One provider failed; the chain should try the next."""


def _synthesize_elevenlabs(text: str, out_path: str, job_id=None) -> None:
    api_key = current_app.config["ELEVENLABS_API_KEY"]
    voice_id = current_app.config["ELEVENLABS_VOICE_ID"]
    if not api_key or not voice_id:
        raise ProviderUnavailable("ElevenLabs is not configured")

    def _call():
        resp = requests.post(
            ELEVENLABS_TTS_URL.format(voice_id=voice_id),
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.content

    try:
        audio = call_with_resilience(
            _call, service="elevenlabs", max_attempts=3, base_delay=1.5, job_id=job_id
        )
    except (CircuitOpenError, requests.RequestException) as exc:
        raise ProviderUnavailable(str(exc)) from exc

    with open(out_path, "wb") as f:
        f.write(audio)


def _synthesize_edge(text: str, out_path: str, job_id=None) -> None:
    try:
        import edge_tts
    except ImportError as exc:
        raise ProviderUnavailable(
            "edge-tts is not installed (pip install edge-tts)"
        ) from exc

    voice = current_app.config["TTS_FALLBACK_VOICE"]

    async def _run():
        await asyncio.wait_for(
            edge_tts.Communicate(text, voice).save(out_path),
            timeout=EDGE_TIMEOUT_SECONDS,
        )

    try:
        # Runs on a render worker thread, which has no event loop of its own,
        # so a fresh one per call is safe.
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - any failure just means "next provider"
        raise ProviderUnavailable(f"edge-tts failed: {exc}") from exc

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise ProviderUnavailable("edge-tts produced no audio")


PROVIDERS = {
    "elevenlabs": _synthesize_elevenlabs,
    "edge": _synthesize_edge,
}


def provider_chain() -> list[str]:
    """Providers to try, in order. ElevenLabs is skipped entirely when it has
    no key, so an unconfigured install goes straight to the free fallback."""
    chain = []
    if current_app.config.get("ELEVENLABS_API_KEY") and current_app.config.get(
        "ELEVENLABS_VOICE_ID"
    ):
        chain.append("elevenlabs")

    fallback = (current_app.config.get("TTS_FALLBACK_PROVIDER") or "none").strip().lower()
    if fallback in PROVIDERS and fallback not in chain:
        chain.append(fallback)
    elif fallback not in ("none", "") and fallback not in PROVIDERS:
        logger.warning("unknown TTS_FALLBACK_PROVIDER %r -- ignoring", fallback)
    return chain


def synthesize_speech(text: str, out_path: str, job_id=None) -> str:
    """Write an mp3 of `text` to `out_path`, returning the provider that
    produced it. Raises TTSUnavailableError if every provider failed."""
    chain = provider_chain()
    if not chain:
        raise TTSUnavailableError(
            "No TTS provider configured. Set ELEVENLABS_API_KEY, or "
            "TTS_FALLBACK_PROVIDER=edge for the free fallback."
        )

    failures = []
    for name in chain:
        try:
            PROVIDERS[name](text, out_path, job_id=job_id)
            logger.info("job=%s narrated with %s", job_id, name)
            return name
        except ProviderUnavailable as exc:
            logger.warning("job=%s TTS provider %s unavailable: %s", job_id, name, exc)
            failures.append(f"{name}: {exc}")

    raise TTSUnavailableError("; ".join(failures))
