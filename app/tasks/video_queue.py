"""Background video rendering, without a broker or a second process.

Video jobs run on a small thread pool inside the web process. Rendering is
dominated by waiting on subprocesses (Manim, ffmpeg) and HTTP APIs (Gemini,
ElevenLabs), all of which release the GIL, so threads are a good fit and the
whole app stays a single `python run.py`.

Progress is still tracked the same way it was under Celery -- each stage
commits to the VideoJob row -- so the polling and SSE endpoints are
unchanged. A job's status living in the database rather than in a broker
also means an interrupted render is visible as a stuck row rather than a
silently lost message.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from flask import current_app

logger = logging.getLogger("mathverse.tasks.video")

# One at a time: a Manim render is CPU- and memory-hungry, and queueing is
# friendlier than thrashing a laptop with several concurrent renders.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mv-video")


def enqueue_video(job_id: int) -> None:
    """Schedule `job_id` for rendering and return immediately."""
    app = current_app._get_current_object()
    _executor.submit(_run_job, app, job_id)
    logger.info("job=%s queued for rendering", job_id)


def _run_job(app, job_id: int) -> None:
    import shutil
    import tempfile

    from app.extensions import db
    from app.models.video import VideoJob
    from app.tasks.video_tasks import _run_pipeline, prompt_cache_key, _find_cached_job

    with app.app_context():
        job = db.session.get(VideoJob, job_id)
        if job is None:
            logger.error("video job %s not found", job_id)
            return

        job.prompt_hash = prompt_cache_key(job.prompt)
        db.session.commit()

        cached = _find_cached_job(job.prompt_hash)
        if cached is not None and cached.id != job.id:
            from datetime import datetime, timezone

            job.video_path = cached.video_path
            job.manim_code = cached.manim_code
            job.narration_json = cached.narration_json
            job.duration_sec = cached.duration_sec
            job.status = "done"
            job.stage_progress = 100
            job.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            logger.info("job=%s served from cache of job=%s", job.id, cached.id)
            return

        work_dir = tempfile.mkdtemp(prefix=f"mv-job-{job.id}-")
        try:
            _run_pipeline(job, work_dir)
        except Exception:
            logger.exception("job=%s crashed", job_id)
            from app.tasks.video_tasks import _fail_with_fallback

            _fail_with_fallback(job, "the render crashed unexpectedly")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


def shutdown(wait: bool = False) -> None:
    _executor.shutdown(wait=wait, cancel_futures=not wait)
