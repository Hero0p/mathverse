import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.tiers import tier_for_elo


def _utcnow():
    return datetime.now(timezone.utc)


def generate_guest_handle() -> str:
    return f"Guest-{secrets.token_hex(3)}"


def generate_guest_token() -> str:
    return secrets.token_urlsafe(32)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=True, index=True)
    password_hash = db.Column(db.String(255), nullable=True)
    is_guest = db.Column(db.Boolean, default=False, nullable=False)
    guest_token = db.Column(db.String(64), unique=True, nullable=True, index=True)
    display_name = db.Column(db.String(64), nullable=False)

    elo = db.Column(db.Integer, nullable=True)  # None until placement
    tier = db.Column(db.String(32), nullable=True)
    peak_elo = db.Column(db.Integer, nullable=True)
    placement_done = db.Column(db.Boolean, default=False, nullable=False)
    matches_played = db.Column(db.Integer, default=0, nullable=False)
    wins = db.Column(db.Integer, default=0, nullable=False)
    losses = db.Column(db.Integer, default=0, nullable=False)
    draws = db.Column(db.Integer, default=0, nullable=False)
    floor_shields_used = db.Column(db.Text, default="", nullable=False)  # csv of tier names

    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    last_seen = db.Column(db.DateTime, default=_utcnow, nullable=False)

    conversations = db.relationship("Conversation", backref="user", lazy="dynamic")
    video_jobs = db.relationship("VideoJob", backref="user", lazy="dynamic")
    whiteboards = db.relationship("Whiteboard", backref="user", lazy="dynamic")

    # --- password -----------------------------------------------------
    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    # --- factory helpers -----------------------------------------------
    @classmethod
    def create_guest(cls) -> "User":
        handle = generate_guest_handle()
        # guarantee uniqueness even under a rare collision
        while cls.query.filter_by(username=handle).first() is not None:
            handle = generate_guest_handle()
        guest = cls(
            username=handle,
            display_name=handle,
            is_guest=True,
            guest_token=generate_guest_token(),
        )
        return guest

    def claim(self, email: str, password: str, username: str | None = None) -> None:
        """Convert this guest row into a full account in place."""
        if not self.is_guest:
            raise ValueError("Only guest accounts can be claimed.")
        self.email = email
        self.set_password(password)
        if username:
            self.username = username
        self.is_guest = False
        self.guest_token = None

    # --- ranking helpers -------------------------------------------------
    @property
    def is_ranked_visible(self) -> bool:
        """Guests never appear on the public leaderboard."""
        return not self.is_guest and self.elo is not None

    @property
    def tier_obj(self):
        return tier_for_elo(self.elo)

    def has_used_shield(self, tier_name: str) -> bool:
        return tier_name in (self.floor_shields_used or "").split(",")

    def mark_shield_used(self, tier_name: str) -> None:
        used = set(filter(None, (self.floor_shields_used or "").split(",")))
        used.add(tier_name)
        self.floor_shields_used = ",".join(sorted(used))

    def touch(self) -> None:
        self.last_seen = _utcnow()

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "is_guest": self.is_guest,
            "elo": self.elo,
            "tier": self.tier,
            "peak_elo": self.peak_elo,
            "matches_played": self.matches_played,
            "wins": self.wins,
            "losses": self.losses,
            "draws": self.draws,
        }

    def __repr__(self):
        return f"<User {self.username} guest={self.is_guest}>"
