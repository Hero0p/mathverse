"""The full text -> video Celery pipeline described in spec section 5.

Stages: scripting -> rendering (with up to MAX_REPAIR_ATTEMPTS self-repair
round trips) -> narrating -> voicing -> muxing -> done. On any
unrecoverable failure the job still gets a text+KaTeX fallback solution so
the user is never left with nothing.
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from flask import current_app

from app.extensions import db
from app.models.video import VideoJob
from app.services import gemini_service, manim_service, media_service, tts_service
from app.services.manim_service import RenderError
from app.services.script_validator import ScriptValidationError
from app.services.tts_service import TTSUnavailableError

logger = logging.getLogger("mathverse.tasks.video")


def _set_stage(job: VideoJob, status: str, progress: int) -> None:
    job.status = status
    job.stage_progress = progress
    db.session.commit()


def prompt_cache_key(prompt: str) -> str:
    normalized = " ".join(prompt.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _find_cached_job(prompt_hash: str) -> VideoJob | None:
    return (
        VideoJob.query.filter_by(prompt_hash=prompt_hash, status="done")
        .filter(VideoJob.video_path.isnot(None))
        .order_by(VideoJob.completed_at.desc())
        .first()
    )


def _run_pipeline(job: VideoJob, work_dir: str) -> None:
    """The full scripting -> rendering -> narration -> voicing -> muxing run.

    Invoked on a background thread by app/tasks/video_queue.py, which owns
    the prompt-cache check and the temp-directory lifecycle.
    """
    max_repairs = current_app.config["MAX_REPAIR_ATTEMPTS"]


    # --- scripting -------------------------------------------------------
    _set_stage(job, "scripting", 5)
    try:
        script = gemini_service.generate_manim_script(job.prompt, job_id=job.id)
    except Exception as exc:
        return _fail_with_fallback(job, f"scripting failed: {exc}")

    job.manim_code = script
    db.session.commit()

    # --- rendering with self-repair --------------------------------------
    _set_stage(job, "rendering", 20)
    clips = None
    last_error = None
    for attempt in range(max_repairs + 1):
        job.attempts = attempt + 1
        db.session.commit()
        try:
            clips = manim_service.render_scene(script, job.id, work_dir=os.path.join(work_dir, f"try{attempt}"))
            break
        except ScriptValidationError as exc:
            # Never retry a script that failed the AST allowlist -- ask for
            # a fresh generation instead of "repairing" unsafe code.
            last_error = str(exc)
            logger.error("job=%s script failed validation: %s", job.id, exc)
            break
        except RenderError as exc:
            last_error = exc.traceback_text
            logger.warning("job=%s render attempt %d failed: %s", job.id, attempt, exc)
            if attempt < max_repairs:
                try:
                    script = gemini_service.repair_manim_script(
                        script, exc.traceback_text, job_id=job.id, timed_out=exc.timed_out
                    )
                    job.manim_code = script
                    db.session.commit()
                except Exception as repair_exc:
                    last_error = f"{last_error}\n(repair call failed: {repair_exc})"
                    break

    if clips is None:
        return _fail_with_fallback(job, last_error or "rendering failed after all repair attempts")

    # --- narrating ---------------------------------------------------------
    _set_stage(job, "narrating", 55)
    try:
        narration = gemini_service.generate_narration(script, job_id=job.id)
    except Exception as exc:
        logger.warning("job=%s narration generation failed, shipping silent video: %s", job.id, exc)
        narration = []

    job.narration_json = json.dumps(narration)
    db.session.commit()

    # --- voicing -------------------------------------------------------
    _set_stage(job, "voicing", 70)
    audio_paths = []
    tts_ok = bool(narration)
    voice_provider = None
    if tts_ok:
        for i, beat in enumerate(narration):
            audio_path = os.path.join(work_dir, f"beat_{i}.mp3")
            try:
                voice_provider = tts_service.synthesize_speech(
                    beat["text"], audio_path, job_id=job.id
                )
                audio_paths.append(audio_path)
            except TTSUnavailableError as exc:
                logger.warning("job=%s TTS unavailable, shipping silent video: %s", job.id, exc)
                tts_ok = False
                break

    # --- muxing -------------------------------------------------------
    _set_stage(job, "muxing", 85)
    out_dir = os.path.join(current_app.config["MEDIA_ROOT"], "videos")
    os.makedirs(out_dir, exist_ok=True)
    final_path = os.path.join(out_dir, f"{job.id}.mp4")

    try:
        if tts_ok and len(audio_paths) == len(clips):
            padded_clips = []
            for i, (clip, audio) in enumerate(zip(clips, audio_paths)):
                duration = media_service.probe_duration(audio)
                padded = os.path.join(work_dir, f"padded_{i}.mp4")
                media_service.pad_video_to_length(clip, duration, padded)
                padded_clips.append(padded)

            concat_video_path = os.path.join(work_dir, "concat_video.mp4")
            concat_audio_path = os.path.join(work_dir, "concat_audio.mp3")
            media_service.concat_videos(padded_clips, concat_video_path)
            media_service.concat_audio(audio_paths, concat_audio_path)
            media_service.mux_video_audio(concat_video_path, concat_audio_path, final_path)
            job.duration_sec = media_service.probe_duration(final_path)
        else:
            concat_video_path = os.path.join(work_dir, "concat_video.mp4")
            media_service.concat_videos(clips, concat_video_path)
            media_service.mux_silent(concat_video_path, final_path)
            job.duration_sec = media_service.probe_duration(final_path)
            job.fallback_text = (
                "Voice narration wasn't available for this video, so it's silent. "
                "The on-screen steps are still complete and correct."
            )
    except media_service.MediaError as exc:
        return _fail_with_fallback(job, f"muxing failed: {exc}")

    job.video_path = final_path
    _set_stage(job, "done", 100)
    job.completed_at = datetime.now(timezone.utc)
    db.session.commit()
    logger.info(
        "job=%s completed in %s attempts (voice: %s)",
        job.id, job.attempts, voice_provider or "none",
    )


def _fail_with_fallback(job: VideoJob, error: str) -> None:
    # Keep the TAIL: a Python traceback lands at the end of the output, so
    # truncating from the front would store only the preamble.
    job.error_log = error[-8000:]
    try:
        job.fallback_text = gemini_service.generate_text_solution(job.prompt, job_id=job.id)
    except Exception as exc:
        logger.error("job=%s fallback text generation also failed: %s", job.id, exc)
        job.fallback_text = (
            "We couldn't render a video or a text solution for this prompt right now. "
            "Please try rephrasing your question."
        )
    _set_stage(job, "failed", 100)
    logger.error("job=%s failed: %s", job.id, error)
