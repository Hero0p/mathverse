from datetime import datetime, timezone

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class Match(db.Model):
    __tablename__ = "matches"

    id = db.Column(db.Integer, primary_key=True)
    format = db.Column(db.Integer, nullable=False)  # 30 | 60 (seconds)
    status = db.Column(db.String(16), default="pending", nullable=False)  # pending|live|finished|abandoned
    is_ranked = db.Column(db.Boolean, default=True, nullable=False)
    is_provisional = db.Column(db.Boolean, default=False, nullable=False)

    player_a_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    player_b_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)  # null vs bot

    question_ids_json = db.Column(db.Text, nullable=False, default="[]")

    score_a = db.Column(db.Integer, default=0, nullable=False)
    score_b = db.Column(db.Integer, default=0, nullable=False)
    wrong_a = db.Column(db.Integer, default=0, nullable=False)
    wrong_b = db.Column(db.Integer, default=0, nullable=False)
    time_ms_a = db.Column(db.Integer, default=0, nullable=False)
    time_ms_b = db.Column(db.Integer, default=0, nullable=False)

    winner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    is_draw = db.Column(db.Boolean, default=False, nullable=False)

    elo_before_a = db.Column(db.Integer, nullable=True)
    elo_before_b = db.Column(db.Integer, nullable=True)
    elo_delta_a = db.Column(db.Integer, nullable=True)
    elo_delta_b = db.Column(db.Integer, nullable=True)

    started_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    player_a = db.relationship("User", foreign_keys=[player_a_id])
    player_b = db.relationship("User", foreign_keys=[player_b_id])
    winner = db.relationship("User", foreign_keys=[winner_id])
    answers = db.relationship("MatchAnswer", backref="match", lazy="dynamic")

    def to_dict(self):
        return {
            "id": self.id,
            "format": self.format,
            "status": self.status,
            "is_ranked": self.is_ranked,
            "player_a": self.player_a.to_public_dict() if self.player_a else None,
            "player_b": self.player_b.to_public_dict() if self.player_b else None,
            "score_a": self.score_a,
            "score_b": self.score_b,
            "winner_id": self.winner_id,
            "is_draw": self.is_draw,
            "elo_delta_a": self.elo_delta_a,
            "elo_delta_b": self.elo_delta_b,
        }


class MatchAnswer(db.Model):
    __tablename__ = "match_answers"

    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(db.Integer, db.ForeignKey("matches.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False)
    answer_given = db.Column(db.String(512), nullable=True)
    is_correct = db.Column(db.Boolean, default=False, nullable=False)
    is_skip = db.Column(db.Boolean, default=False, nullable=False)
    ms_elapsed = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
