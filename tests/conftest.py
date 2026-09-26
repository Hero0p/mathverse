import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app import create_app
from app.extensions import db as _db
from app.extensions import get_store


@pytest.fixture()
def app():
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        # The ephemeral store is a module-level singleton, so rate-limit
        # counters and queue entries would otherwise leak between tests.
        get_store().flushall()
        yield application
        _db.session.remove()
        _db.drop_all()
        get_store().flushall()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def client(app):
    return app.test_client()
