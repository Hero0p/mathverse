import pytest

from app.extensions import db as _db
from app.models.chat import Conversation, Message
from app.models.user import User
from app.models.whiteboard import Whiteboard


def test_create_guest_has_random_handle_and_token(app):
    guest = User.create_guest()
    assert guest.is_guest is True
    assert guest.username.startswith("Guest-")
    assert guest.guest_token is not None
    assert guest.password_hash is None
    assert guest.email is None


def test_guest_create_avoids_username_collisions(app, monkeypatch):
    _db.session.add(User.create_guest())
    _db.session.commit()
    existing_username = User.query.first().username

    calls = iter([existing_username, "Guest-unique1"])
    monkeypatch.setattr("app.models.user.generate_guest_handle", lambda: next(calls))

    second = User.create_guest()
    assert second.username == "Guest-unique1"


def test_claim_converts_guest_in_place_preserving_id(app):
    guest = User.create_guest()
    _db.session.add(guest)
    _db.session.commit()
    guest_id = guest.id

    guest.claim(email="student@example.com", password="supersecret1")
    _db.session.commit()

    reloaded = _db.session.get(User, guest_id)
    assert reloaded.id == guest_id
    assert reloaded.is_guest is False
    assert reloaded.email == "student@example.com"
    assert reloaded.guest_token is None
    assert reloaded.check_password("supersecret1") is True


def test_claim_preserves_conversations_boards_and_elo(app):
    guest = User.create_guest()
    guest.elo = 950
    guest.tier = "Genius"
    guest.placement_done = True
    guest.matches_played = 3
    guest.wins = 2
    _db.session.add(guest)
    _db.session.commit()

    conv = Conversation(user_id=guest.id, title="Quadratics")
    _db.session.add(conv)
    _db.session.commit()
    _db.session.add(Message(conversation_id=conv.id, role="user", content="Explain the quadratic formula"))
    board = Whiteboard(user_id=guest.id, title="Scratch pad", canvas_json="{}")
    _db.session.add(board)
    _db.session.commit()

    guest.claim(email="upgraded@example.com", password="supersecret1")
    _db.session.commit()

    user_id = guest.id
    convs = Conversation.query.filter_by(user_id=user_id).all()
    boards = Whiteboard.query.filter_by(user_id=user_id).all()
    reloaded = _db.session.get(User, user_id)

    assert len(convs) == 1 and convs[0].title == "Quadratics"
    assert convs[0].messages.count() == 1
    assert len(boards) == 1 and boards[0].title == "Scratch pad"
    assert reloaded.elo == 950
    assert reloaded.tier == "Genius"
    assert reloaded.matches_played == 3
    assert reloaded.wins == 2


def test_claim_rejects_non_guest_accounts(app):
    user = User(username="already_real", display_name="Already Real")
    user.email = "real@example.com"
    user.set_password("supersecret1")
    _db.session.add(user)
    _db.session.commit()

    with pytest.raises(ValueError):
        user.claim(email="new@example.com", password="anotherpassword")


def test_guest_is_never_leaderboard_visible(app):
    guest = User.create_guest()
    guest.elo = 2500
    assert guest.is_ranked_visible is False


def test_registered_user_with_elo_is_leaderboard_visible(app):
    user = User(username="ranked_player", display_name="Ranked Player")
    user.elo = 1500
    assert user.is_ranked_visible is True
