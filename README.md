# MathVerse

A math-learning platform with three pillars:

1. **Ask** — any question gets an instant text answer, and optionally a narrated animated video.
2. **Note** — an infinite whiteboard canvas with an AI tutor attached.
3. **Practice** — ranked 30s/60s math duels on a live Elo ladder.

Every feature works via **Continue as Guest** — an account is never required, only offered.

## Running it

No Docker, no Redis, no background worker, no second terminal:

```bash
python -m venv .venv
.venv\Scripts\activate            # source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

python run.py
```

Then open **http://localhost:5000**.

`run.py` loads `.env` (creating it from `.env.example` on first run), applies database
migrations, seeds the Arena question bank if empty, reports which optional features are
available, and serves the app. Ctrl+C stops it.

```
python run.py --port 5055     # different port
python run.py --debug         # Flask debug pages + reloader
```

### Configuration

Only one key really matters. Put it in `.env`:

| Variable | Needed for | Without it |
|---|---|---|
| `GEMINI_API_KEY` | Chat answers, whiteboard AI, video scripts | Those features fail; Arena still works ([get a free key](https://aistudio.google.com/apikey)) |
| `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` | Best-quality narration | Falls back automatically to free voices — see below |
| `SECRET_KEY` | Session signing | Falls back to a dev default |

`DATABASE_URL` defaults to SQLite in `instance/`. `MANIM_QUALITY` (`low_quality` →
`production_quality`) trades render time for resolution — start low.

### Narration and its fallback

TTS providers are tried in order until one produces audio:

1. **ElevenLabs** — best quality, needs a paid key. Skipped entirely if unconfigured.
2. **`TTS_FALLBACK_PROVIDER`** — `edge` by default: Microsoft's neural voices via
   [`edge-tts`](https://github.com/rany2/edge-tts). Free, no API key, good quality. It is a
   cloud service rather than a local model, so it needs an internet connection.

So an expired, invalid, or out-of-quota ElevenLabs key costs you nothing — the render just
uses the free voice and logs which provider it used. If *every* provider fails, the video
still ships silently with on-screen text rather than failing.

Change the voice with `TTS_FALLBACK_VOICE` in `.env` (e.g. `en-US-GuyNeural`,
`en-GB-SoniaNeural`, `en-IN-NeerjaNeural`). List all of them with:

```bash
edge-tts --list-voices
```

Set `TTS_FALLBACK_PROVIDER=none` to disable the fallback entirely.

### Video rendering (optional)

Chat, the whiteboard, and the Arena all work without this. To render videos you also need
Manim, a LaTeX distribution, and ffmpeg:

```bash
pip install -r requirements-render.txt
# plus: ffmpeg on PATH, and TeX Live or MiKTeX (providing latex + dvisvgm)
```

`run.py` tells you at startup whether these were found. Renders run on a background thread
in-process, one at a time.

## Architecture

- **Flask** application factory (`app/__init__.py`) with a blueprint per feature area
  (`auth`, `chat`, `whiteboard`, `arena`, `main`).
- **Chat answers in two steps.** `POST /api/conversations/<id>/message` calls Gemini
  synchronously and returns text immediately. Rendering a video is a separate, explicit
  `POST /api/messages/<id>/video`, so the common case is fast and the small daily video
  budget is only spent when asked for.
- **Background work without a broker.** `app/tasks/video_queue.py` runs the render pipeline
  on a thread pool. Progress is committed to the `VideoJob` row at each stage, so polling
  and SSE work unchanged — and an interrupted render is a visible stuck row rather than a
  lost message.
- **Ephemeral state without a cache server.** `app/services/store.py` is an in-process store
  implementing the slice of the Redis API the app uses (strings, hashes, sorted sets, lists,
  sets, TTLs, pipelines). It backs rate limits, the matchmaking queue, live placement
  quizzes, and the cached "Infinite" title.
- **Flask-SocketIO** drives the live Arena match loop (`app/sockets/arena_events.py`). The
  server is the sole source of truth for the clock, question order, and correctness —
  answers are never sent to the client.
- **Self-repairing renders.** A failed render sends the traceback back to Gemini for repair
  (up to `MAX_REPAIR_ATTEMPTS`). Timeouts take a different path that asks the model to cut
  scope, since there is no bug to fix. If everything fails, the user still gets a text
  solution rather than nothing.

## ⚠️ This build is local-only

Manim runs **in-process, unsandboxed**. Generated scripts are model-authored code, and the
AST allowlist in `app/services/script_validator.py` — which rejects imports and calls outside
`manim`/`numpy`/`math`, plus filesystem and dunder access — is the only thing between a
generated script and your machine. That is defense in depth, not a sandbox.

This is a deliberate trade-off to remove the Docker dependency for single-user local use.
`ProdConfig` refuses to start for this reason. Before serving anyone else's prompts, restore
containerized rendering: no network, read-only root filesystem, non-root user, and
memory/cpu/pid limits.

## Question ingestion

`scripts/ingest_questions.py` takes a MATH/QSA-style directory of JSON files or a single
JSONL file. Idempotent and resumable; malformed LaTeX is quarantined rather than failing
the run.

```bash
python scripts/ingest_questions.py --path /path/to/dataset --limit 5000
python scripts/ingest_questions.py --path /path/to/dataset --dry-run
```

## Layout

```
app/
  blueprints/   auth, chat, whiteboard, arena, main
  models/       User, Conversation/Message, VideoJob, Whiteboard, Question, Match/MatchAnswer
  services/     gemini, manim, tts, media, elo, matchmaking, arena, grading, store, resilience
  prompts/      system prompts for scripting / narration / repair / whiteboard
  tasks/        the render pipeline and its in-process queue
  sockets/      the Arena Socket.IO protocol
  templates/    Jinja2 + Tailwind (CDN) + vanilla JS; Fabric.js whiteboard; KaTeX
scripts/        question ingestion + dev seeding
tests/          pytest suite
run.py          the only command you need
```

AUTHOR : NISHANT CHAUDHARY