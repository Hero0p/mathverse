import json
import time

from flask import Response, current_app, jsonify, render_template, request, stream_with_context
from flask_login import current_user, login_required

from app.blueprints.chat import chat_bp
from app.extensions import db
from app.models.chat import Conversation, Message
from app.models.video import VideoJob
from app.services import gemini_service, rate_limit_service
from app.tasks.video_queue import enqueue_video


@chat_bp.route("/chat")
@login_required
def chat_page():
    return render_template("chat/chat.html")


@chat_bp.route("/api/conversations", methods=["GET"])
@login_required
def list_conversations():
    conversations = (
        Conversation.query.filter_by(user_id=current_user.id)
        .order_by(Conversation.updated_at.desc())
        .all()
    )
    return jsonify(ok=True, data=[c.to_dict() for c in conversations], error=None)


@chat_bp.route("/api/conversations", methods=["POST"])
@login_required
def create_conversation():
    title = (request.json or {}).get("title") or "New conversation"
    conversation = Conversation(user_id=current_user.id, title=title)
    db.session.add(conversation)
    db.session.commit()
    return jsonify(ok=True, data=conversation.to_dict(), error=None), 201


@chat_bp.route("/api/conversations/<int:conversation_id>/messages", methods=["GET"])
@login_required
def list_messages(conversation_id):
    conversation = Conversation.query.filter_by(
        id=conversation_id, user_id=current_user.id
    ).first_or_404()
    return jsonify(ok=True, data=[m.to_dict() for m in conversation.messages], error=None)


@chat_bp.route("/api/conversations/<int:conversation_id>/message", methods=["POST"])
@login_required
def post_message(conversation_id):
    """Answer the question synchronously with text. Video rendering is a
    separate, explicit action (see start_video below) so the common case
    stays fast and doesn't depend on a running Celery worker."""
    conversation = Conversation.query.filter_by(
        id=conversation_id, user_id=current_user.id
    ).first_or_404()

    prompt = ((request.json or {}).get("content") or "").strip()
    if not prompt:
        return jsonify(ok=False, data=None, error="Message content is required."), 400

    if not rate_limit_service.check_and_increment(current_user, "chat"):
        return jsonify(
            ok=False,
            data=None,
            error="Daily question limit reached. Create an account for a higher limit.",
        ), 429

    user_msg = Message(conversation_id=conversation.id, role="user", content=prompt)
    db.session.add(user_msg)

    if conversation.title == "New conversation":
        conversation.title = prompt[:60]
    db.session.commit()

    try:
        answer = gemini_service.generate_chat_answer(prompt, job_id=f"chat-{conversation.id}")
    except Exception as exc:  # noqa: BLE001 - surface any provider failure to the user
        current_app.logger.exception("chat answer failed for conversation %s", conversation.id)
        return jsonify(
            ok=False, data=None, error=f"The AI tutor is unavailable right now: {exc}"
        ), 502

    assistant_msg = Message(
        conversation_id=conversation.id, role="assistant", content=answer
    )
    db.session.add(assistant_msg)
    db.session.commit()

    return jsonify(
        ok=True,
        data={"user_message": user_msg.to_dict(), "message": assistant_msg.to_dict()},
        error=None,
    ), 201


@chat_bp.route("/api/messages/<int:message_id>/video", methods=["POST"])
@login_required
def start_video(message_id):
    """Kick off a video render for the question this assistant message
    answered. Charged against the (much smaller) daily video budget."""
    message = (
        Message.query.join(Conversation)
        .filter(Message.id == message_id, Conversation.user_id == current_user.id)
        .first_or_404()
    )

    if message.video_job_id:
        job = db.session.get(VideoJob, message.video_job_id)
        if job is not None and job.status != "failed":
            # Already requested -- hand back the existing job instead of
            # paying for a duplicate render.
            return jsonify(ok=True, data={"job_id": job.id}, error=None)

    prompt = _prompt_for(message)
    if not prompt:
        return jsonify(ok=False, data=None, error="Could not find the original question."), 400

    if not rate_limit_service.check_and_increment(current_user, "video"):
        return jsonify(
            ok=False,
            data=None,
            error="Daily video limit reached. Create an account for a higher limit.",
        ), 429

    job = VideoJob(user_id=current_user.id, prompt=prompt)
    db.session.add(job)
    db.session.flush()
    message.video_job_id = job.id
    db.session.commit()

    enqueue_video(job.id)
    return jsonify(ok=True, data={"job_id": job.id}, error=None), 202


def _prompt_for(assistant_message: Message) -> str | None:
    """The user question immediately preceding an assistant reply."""
    if assistant_message.role == "user":
        return assistant_message.content
    previous = (
        Message.query.filter(
            Message.conversation_id == assistant_message.conversation_id,
            Message.role == "user",
            Message.id < assistant_message.id,
        )
        .order_by(Message.id.desc())
        .first()
    )
    return previous.content if previous else None


@chat_bp.route("/api/video/<int:job_id>/status", methods=["GET"])
@login_required
def video_status(job_id):
    job = VideoJob.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    return jsonify(ok=True, data=job.to_status_dict(), error=None)


@chat_bp.route("/api/video/<int:job_id>/stream")
@login_required
def video_stream(job_id):
    """SSE progress stream, polling the DB row server-side so the client
    doesn't have to."""
    user_id = current_user.id

    def event_stream():
        last_status = None
        for _ in range(int(current_app.config["MAX_RENDER_SECONDS"]) * 2 + 60):
            job = VideoJob.query.filter_by(id=job_id, user_id=user_id).first()
            if job is None:
                yield f"event: error\ndata: {json.dumps({'error': 'not found'})}\n\n"
                return
            data = job.to_status_dict()
            payload = json.dumps(data)
            if payload != last_status:
                yield f"data: {payload}\n\n"
                last_status = payload
            if job.status in ("done", "failed"):
                return
            time.sleep(1.5)

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")
