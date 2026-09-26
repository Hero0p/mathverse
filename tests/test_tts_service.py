"""TTS provider chain.

The property that matters: a broken, expired, or unconfigured ElevenLabs key
must quietly fall through to the free provider instead of costing the user
their narration -- and if everything fails, the caller still gets a clean
TTSUnavailableError so the video ships silently rather than failing.
"""
import pytest

from app.services import tts_service
from app.services.tts_service import ProviderUnavailable, TTSUnavailableError


@pytest.fixture()
def configured(app):
    app.config["ELEVENLABS_API_KEY"] = "key"
    app.config["ELEVENLABS_VOICE_ID"] = "voice"
    app.config["TTS_FALLBACK_PROVIDER"] = "edge"
    app.config["TTS_FALLBACK_VOICE"] = "en-US-AriaNeural"
    return app


# --- chain composition ----------------------------------------------------
def test_chain_prefers_elevenlabs_then_fallback(configured):
    with configured.test_request_context():
        assert tts_service.provider_chain() == ["elevenlabs", "edge"]


def test_unconfigured_elevenlabs_is_skipped_entirely(configured):
    """No key means no wasted call -- straight to the free provider."""
    configured.config["ELEVENLABS_API_KEY"] = ""
    with configured.test_request_context():
        assert tts_service.provider_chain() == ["edge"]


def test_missing_voice_id_also_skips_elevenlabs(configured):
    configured.config["ELEVENLABS_VOICE_ID"] = ""
    with configured.test_request_context():
        assert tts_service.provider_chain() == ["edge"]


def test_fallback_none_disables_the_fallback(configured):
    configured.config["TTS_FALLBACK_PROVIDER"] = "none"
    with configured.test_request_context():
        assert tts_service.provider_chain() == ["elevenlabs"]


def test_unknown_fallback_is_ignored_not_fatal(configured):
    configured.config["TTS_FALLBACK_PROVIDER"] = "nonexistent-engine"
    with configured.test_request_context():
        assert tts_service.provider_chain() == ["elevenlabs"]


def test_fallback_is_case_and_whitespace_tolerant(configured):
    configured.config["TTS_FALLBACK_PROVIDER"] = "  EDGE  "
    with configured.test_request_context():
        assert tts_service.provider_chain() == ["elevenlabs", "edge"]


# --- failover behaviour ---------------------------------------------------
def test_falls_back_when_elevenlabs_rejects_the_key(configured, monkeypatch, tmp_path):
    """The real-world case: a 402/401 from an expired or unpaid key."""
    def _payment_required(text, out_path, job_id=None):
        raise ProviderUnavailable("402 Client Error: Payment Required")

    written = {}

    def _edge(text, out_path, job_id=None):
        written["text"] = text
        open(out_path, "wb").write(b"audio")

    monkeypatch.setitem(tts_service.PROVIDERS, "elevenlabs", _payment_required)
    monkeypatch.setitem(tts_service.PROVIDERS, "edge", _edge)

    out = tmp_path / "beat.mp3"
    with configured.test_request_context():
        used = tts_service.synthesize_speech("hello world", str(out))

    assert used == "edge"
    assert written["text"] == "hello world"
    assert out.read_bytes() == b"audio"


def test_primary_is_used_when_it_works(configured, monkeypatch, tmp_path):
    monkeypatch.setitem(
        tts_service.PROVIDERS, "elevenlabs",
        lambda text, out_path, job_id=None: open(out_path, "wb").write(b"eleven"),
    )
    monkeypatch.setitem(
        tts_service.PROVIDERS, "edge",
        lambda *a, **k: pytest.fail("fallback must not run when primary succeeds"),
    )

    out = tmp_path / "beat.mp3"
    with configured.test_request_context():
        assert tts_service.synthesize_speech("hi", str(out)) == "elevenlabs"


def test_all_providers_failing_raises_tts_unavailable(configured, monkeypatch, tmp_path):
    """Callers catch this and ship a silent video rather than failing the job."""
    def _dead(name):
        def _fn(text, out_path, job_id=None):
            raise ProviderUnavailable(f"{name} is down")
        return _fn

    monkeypatch.setitem(tts_service.PROVIDERS, "elevenlabs", _dead("elevenlabs"))
    monkeypatch.setitem(tts_service.PROVIDERS, "edge", _dead("edge"))

    with configured.test_request_context():
        with pytest.raises(TTSUnavailableError) as excinfo:
            tts_service.synthesize_speech("hi", str(tmp_path / "b.mp3"))

    # the message names every provider that was tried, for diagnosis
    assert "elevenlabs" in str(excinfo.value)
    assert "edge" in str(excinfo.value)


def test_no_providers_configured_gives_actionable_message(configured, tmp_path):
    configured.config["ELEVENLABS_API_KEY"] = ""
    configured.config["TTS_FALLBACK_PROVIDER"] = "none"

    with configured.test_request_context():
        with pytest.raises(TTSUnavailableError, match="TTS_FALLBACK_PROVIDER=edge"):
            tts_service.synthesize_speech("hi", str(tmp_path / "b.mp3"))


def test_edge_rejects_an_empty_output_file(configured, monkeypatch, tmp_path):
    """A provider that "succeeds" but writes nothing must not be treated as
    working -- that would mux a zero-length track into the video."""
    import asyncio

    out = tmp_path / "empty.mp3"
    out.write_bytes(b"")

    monkeypatch.setitem(
        __import__("sys").modules, "edge_tts",
        type("FakeEdge", (), {"Communicate": lambda *a, **k: None})(),
    )

    def _fake_run(coro):
        coro.close()  # close it, or Python warns it was never awaited
        return None

    monkeypatch.setattr(asyncio, "run", _fake_run)

    with configured.test_request_context():
        with pytest.raises(ProviderUnavailable, match="no audio"):
            tts_service._synthesize_edge("hi", str(out))
