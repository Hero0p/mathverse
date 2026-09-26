from datetime import datetime, timezone

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class Whiteboard(db.Model):
    __tablename__ = "whiteboards"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(255), default="Untitled board", nullable=False)
    canvas_json = db.Column(db.Text, default="{}", nullable=False)
    thumbnail_path = db.Column(db.String(512), nullable=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    def to_dict(self, include_canvas=False):
        data = {
            "id": self.id,
            "title": self.title,
            "thumbnail_url": f"/media/thumbnails/{self.id}.png" if self.thumbnail_path else None,
            "updated_at": self.updated_at.isoformat(),
        }
        if include_canvas:
            data["canvas_json"] = self.canvas_json
        return data
