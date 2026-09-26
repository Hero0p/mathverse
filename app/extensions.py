"""Shared extension instances, initialized in create_app()."""
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

from app.services.store import LocalStore

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")
csrf = CSRFProtect()

# Ephemeral state (rate limits, matchmaking queue, placement quizzes, the
# cached Infinite title set). In-process: MathVerse runs as one local
# process, so there is no broker or cache server to install or start.
_store = LocalStore()


def get_store() -> LocalStore:
    return _store


# Historical name -- plenty of call sites read `r = get_redis()`. The object
# returned implements the same command surface, backed by app/services/store.py.
get_redis = get_store


login_manager.login_view = "main.landing"
login_manager.login_message = None  # guests never see a forced login prompt
