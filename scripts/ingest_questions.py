#!/usr/bin/env python3
"""Ingest the Math QSA dataset into the Question table.

Accepts either:
  - a directory tree of per-problem JSON files (the MATH/QSA dataset layout:
    {"problem": ..., "level": "Level 3", "type": "Algebra", "solution": ...})
  - a single JSONL file, one question object per line

Each record maps onto app.models.question.Question:
  level 1-5, statement, statement_latex, answer, answer_type, choices_json,
  topic, source, external_id.

Idempotent: re-running skips records whose normalized-statement hash already
exists (app.services.question_service.statement_hash). Quarantines any
record whose LaTeX doesn't look well-formed (unbalanced $ or braces) into
a sibling `<path>.quarantine.jsonl` file instead of failing the whole run.

Usage:
    python scripts/ingest_questions.py --path data/MATH --limit 500
    python scripts/ingest_questions.py --path data/questions.jsonl --dry-run
"""
import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.extensions import db
from app.models.question import Question
from app.services.question_service import normalize_answer, statement_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingest_questions")

LEVEL_RE = re.compile(r"(\d)")


def parse_level(raw) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw if 1 <= raw <= 5 else None
    match = LEVEL_RE.search(str(raw))
    if not match:
        return None
    level = int(match.group(1))
    return level if 1 <= level <= 5 else None


def looks_like_valid_latex(text: str | None) -> bool:
    """Cheap structural check standing in for a real KaTeX render pass:
    balanced $ delimiters and balanced braces. Not a full LaTeX parser,
    but catches the vast majority of malformed exports."""
    if not text:
        return True
    if text.count("$") % 2 != 0:
        return False
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if depth < 0:
            return False
    return depth == 0


def iter_records(path: Path):
    if path.is_dir():
        for json_file in sorted(path.rglob("*.json")):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    record = json.load(f)
                record.setdefault("external_id", json_file.stem)
                record.setdefault("source", str(json_file.relative_to(path)))
                yield record
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("skipping unreadable file %s: %s", json_file, exc)
    else:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning("skipping malformed line %d: %s", i, exc)


def normalize_record(record: dict) -> dict | None:
    statement = record.get("problem") or record.get("statement") or record.get("question")
    if not statement:
        return None
    level = parse_level(record.get("level"))
    if level is None:
        return None
    answer = record.get("answer") or record.get("solution_answer")
    if answer is None:
        return None

    choices = record.get("choices")
    answer_type = "mcq" if choices else record.get("answer_type", "exact")
    if answer_type not in ("mcq", "numeric", "exact"):
        answer_type = "exact"

    return {
        "external_id": str(record.get("external_id") or record.get("id") or ""),
        "level": level,
        "statement": statement.strip(),
        "statement_latex": record.get("statement_latex") or statement.strip(),
        "answer": normalize_answer(str(answer)) if answer_type != "mcq" else str(answer).strip(),
        "answer_type": answer_type,
        "choices_json": json.dumps(choices) if choices else None,
        "topic": record.get("type") or record.get("topic"),
        "source": record.get("source"),
    }


def run(path: str, limit: int | None, dry_run: bool) -> None:
    app = create_app()
    with app.app_context():
        src = Path(path)
        quarantine_path = src.with_suffix(src.suffix + ".quarantine.jsonl")
        quarantine_file = open(quarantine_path, "w", encoding="utf-8") if not dry_run else None

        seen = 0
        inserted = 0
        skipped_duplicate = 0
        skipped_invalid = 0
        quarantined = 0

        try:
            for raw in iter_records(src):
                if limit is not None and seen >= limit:
                    break
                seen += 1

                normalized = normalize_record(raw)
                if normalized is None:
                    skipped_invalid += 1
                    continue

                if not looks_like_valid_latex(normalized["statement_latex"]):
                    quarantined += 1
                    if quarantine_file:
                        quarantine_file.write(json.dumps(raw) + "\n")
                    continue

                s_hash = statement_hash(normalized["statement"])
                if Question.query.filter_by(statement_hash=s_hash).first() is not None:
                    skipped_duplicate += 1
                    continue

                question = Question(statement_hash=s_hash, **normalized)
                if not dry_run:
                    db.session.add(question)
                    if inserted % 200 == 0:
                        db.session.commit()  # commit in batches so a crash mid-run is resumable
                inserted += 1
        finally:
            if quarantine_file:
                quarantine_file.close()

        if not dry_run:
            db.session.commit()

        logger.info(
            "seen=%d inserted=%d duplicates=%d invalid=%d quarantined=%d dry_run=%s",
            seen, inserted, skipped_duplicate, skipped_invalid, quarantined, dry_run,
        )
        if quarantined and not dry_run:
            logger.info("quarantined records written to %s", quarantine_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, help="dataset directory or JSONL file")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.path, args.limit, args.dry_run)


if __name__ == "__main__":
    main()
