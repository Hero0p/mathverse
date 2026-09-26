"""The chat flow deliberately splits into two steps: a fast synchronous text
answer, and an opt-in video render. These tests pin that split down -- asking
a question must never enqueue a render, and rendering must never be billed
twice for the same answer."""
import pytest

from app.extensions import db as _db
from app.models.chat import Conversation, Message
from app.models.video import VideoJob


@pytest.fixture()
def logged_in(app, client, monkeypatch):
    """A guest session with the external dependencies stubbed: Gemini (no
    network) and the render queue (which would otherwise start a real render
    on a background thread)."""
    monkeypatch.setattr(
        "app.blueprints.chat.routes.gemini_service.generate_chat_answer",
        lambda prompt, job_id=None: f"Answer to: {prompt}",
    )
    enqueued = []
    monkeypatch.setattr(
        "app.blueprints.chat.routes.enqueue_video", lambda job_id: enqueued.append(job_id)
    )
    client.post("/auth/guest")
    client.enqueued = enqueued
    return client


def _new_conversation(client):
    return client.post("/api/conversations", json={}).get_json()["data"]["id"]


def test_asking_returns_text_answer_immediately(logged_in):
    cid = _new_conversation(logged_in)
    res = logged_in.post(f"/api/conversations/{cid}/message", json={"content": "What is 2+2?"})

    assert res.status_code == 201
    body = res.get_json()
    assert body["ok"] is True
    assert body["data"]["message"]["content"] == "Answer to: What is 2+2?"
    assert body["data"]["message"]["role"] == "assistant"


def test_asking_does_not_enqueue_a_video_render(logged_in):
    """The whole point of the split: a question costs a text call, not a render."""
    cid = _new_conversation(logged_in)
    logged_in.post(f"/api/conversations/{cid}/message", json={"content": "Explain vectors"})

    assert VideoJob.query.count() == 0
    assert logged_in.enqueued == []
    assert Message.query.filter(Message.video_job_id.isnot(None)).count() == 0


def test_empty_prompt_rejected(logged_in):
    cid = _new_conversation(logged_in)
    res = logged_in.post(f"/api/conversations/{cid}/message", json={"content": "   "})
    assert res.status_code == 400
    assert res.get_json()["ok"] is False


def test_conversation_title_derives_from_first_question(logged_in):
    cid = _new_conversation(logged_in)
    logged_in.post(f"/api/conversations/{cid}/message", json={"content": "Explain the midpoint theorem"})
    assert _db.session.get(Conversation, cid).title == "Explain the midpoint theorem"


def test_generating_video_creates_job_and_links_message(logged_in):
    cid = _new_conversation(logged_in)
    msg = logged_in.post(
        f"/api/conversations/{cid}/message", json={"content": "Explain the midpoint theorem"}
    ).get_json()["data"]["message"]

    res = logged_in.post(f"/api/messages/{msg['id']}/video")
    assert res.status_code == 202

    job_id = res.get_json()["data"]["job_id"]
    job = _db.session.get(VideoJob, job_id)
    # The render must use the original question, not the assistant's answer.
    assert job.prompt == "Explain the midpoint theorem"
    assert _db.session.get(Message, msg["id"]).video_job_id == job_id
    assert logged_in.enqueued == [job_id]


def test_requesting_video_twice_reuses_the_same_job(logged_in):
    cid = _new_conversation(logged_in)
    msg = logged_in.post(
        f"/api/conversations/{cid}/message", json={"content": "Explain vectors"}
    ).get_json()["data"]["message"]

    first = logged_in.post(f"/api/messages/{msg['id']}/video").get_json()["data"]["job_id"]
    second = logged_in.post(f"/api/messages/{msg['id']}/video").get_json()["data"]["job_id"]

    assert first == second
    assert VideoJob.query.count() == 1
    assert logged_in.enqueued == [first]  # not enqueued a second time


def test_cannot_generate_video_for_another_users_message(app, client, logged_in):
    cid = _new_conversation(logged_in)
    msg = logged_in.post(
        f"/api/conversations/{cid}/message", json={"content": "Private question"}
    ).get_json()["data"]["message"]

    other = app.test_client()
    other.post("/auth/guest")
    assert other.post(f"/api/messages/{msg['id']}/video").status_code == 404


def test_cannot_read_another_users_conversation(app, client, logged_in):
    cid = _new_conversation(logged_in)
    other = app.test_client()
    other.post("/auth/guest")
    assert other.get(f"/api/conversations/{cid}/messages").status_code == 404


def test_provider_failure_returns_502_and_persists_no_assistant_message(app, client, monkeypatch):
    def _boom(prompt, job_id=None):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.blueprints.chat.routes.gemini_service.generate_chat_answer", _boom)
    client.post("/auth/guest")
    cid = _new_conversation(client)

    res = client.post(f"/api/conversations/{cid}/message", json={"content": "hi"})
    assert res.status_code == 502
    assert res.get_json()["ok"] is False
    # the user's own message is kept, but no assistant reply is fabricated
    assert Message.query.filter_by(role="assistant").count() == 0
    assert Message.query.filter_by(role="user").count() == 1
