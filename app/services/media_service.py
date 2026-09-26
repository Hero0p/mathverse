"""ffmpeg orchestration: per-beat pad-to-audio-length, concat, and final mux.

All calls shell out to the `ffmpeg`/`ffprobe` CLI via subprocess with
explicit argument lists (never shell=True, never string interpolation of
user content into a shell command).
"""
import json
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger("mathverse.media")


class MediaError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 120) -> None:
    try:
        subprocess.run(
            cmd, check=True, capture_output=True, timeout=timeout, text=True
        )
    except subprocess.CalledProcessError as exc:
        raise MediaError(f"command failed: {' '.join(cmd)}\n{exc.stderr}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"command timed out: {' '.join(cmd)}") from exc


def probe_duration(path: str) -> float:
    """Return the duration of a media file in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, timeout=30, text=True)
    except subprocess.CalledProcessError as exc:
        raise MediaError(f"ffprobe failed on {path}: {exc.stderr}") from exc
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def pad_video_to_length(video_path: str, target_seconds: float, out_path: str) -> None:
    """Pad `video_path` by cloning its last frame until it reaches
    `target_seconds`. If the video is already longer, it's left untouched
    (copied through) rather than cut, so no visual content is lost."""
    current = probe_duration(video_path)
    if current >= target_seconds:
        _run(["ffmpeg", "-y", "-i", video_path, "-c", "copy", out_path])
        return
    pad_duration = target_seconds - current
    _run([
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"tpad=stop_mode=clone:stop_duration={pad_duration:.3f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        out_path,
    ])


def concat_videos(video_paths: list[str], out_path: str) -> None:
    """Concatenate beat videos (already padded to their narration length)
    into one continuous video using ffmpeg's concat demuxer."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as list_file:
        for path in video_paths:
            escaped = path.replace("'", "'\\''")
            list_file.write(f"file '{escaped}'\n")
        list_path = list_file.name
    try:
        _run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy", out_path,
        ])
    finally:
        os.unlink(list_path)


def concat_audio(audio_paths: list[str], out_path: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as list_file:
        for path in audio_paths:
            escaped = path.replace("'", "'\\''")
            list_file.write(f"file '{escaped}'\n")
        list_path = list_file.name
    try:
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out_path])
    finally:
        os.unlink(list_path)


def mux_video_audio(video_path: str, audio_path: str, out_path: str) -> None:
    """Attach the final concatenated audio track to the final concatenated
    video. Video is re-encoded to keep container/codec consistent; audio is
    copied through."""
    _run([
        "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
        "-c:v", "libx264", "-c:a", "aac", "-shortest", out_path,
    ])


def mux_silent(video_path: str, out_path: str) -> None:
    """Fallback when TTS is unavailable: ship the video with no audio track."""
    _run(["ffmpeg", "-y", "-i", video_path, "-c", "copy", "-an", out_path])
