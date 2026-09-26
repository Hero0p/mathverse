from datetime import datetime, timezone

from app.extensions import db

STATUSES = (
    "queued", "scripting", "rendering", "narrating", "voicing", "muxing", "done", "failed",
)

STAGE_LABELS = {
    "queued": "Queued",
    "scripting": "Writing the animation",
    "rendering": "Rendering",
    "narrating": "Writing narration",
    "voicing": "Recording voice",
    "muxing": "Finishing up",
    "done": "Done",
    "failed": "Failed",
}


def _utcnow():
    return datetime.now(timezone.utc)


class VideoJob(db.Model):
    __tablename__ = "video_jobs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    prompt = db.Column(db.Text, nullable=False)
    prompt_hash = db.Column(db.String(64), nullable=True, index=True)  # for cache reuse
    status = db.Column(db.String(16), default="queued", nullable=False)
    stage_progress = db.Column(db.Integer, default=0, nullable=False)
    manim_code = db.Column(db.Text, nullable=True)
    narration_json = db.Column(db.Text, nullable=True)
    video_path = db.Column(db.String(512), nullable=True)
    error_log = db.Column(db.Text, nullable=True)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    duration_sec = db.Column(db.Float, nullable=True)
    fallback_text = db.Column(db.Text, nullable=True)  # KaTeX step-by-step if video failed
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    def to_status_dict(self):
        return {
            "id": self.id,
            "status": self.status,
            "stage": STAGE_LABELS.get(self.status, self.status),
            "progress": self.stage_progress,
            "video_url": f"/media/videos/{self.id}.mp4" if self.video_path else None,
            "error": self.error_log if self.status == "failed" else None,
            "fallback_text": self.fallback_text,
        }
