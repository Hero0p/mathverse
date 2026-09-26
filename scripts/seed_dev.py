#!/usr/bin/env python3
"""Seed a fresh dev database with enough Question rows to actually play the
Arena, plus a couple of demo users. Safe to re-run (idempotent on the
statement hash, same as the real ingestion script).

Usage: flask seed-dev   (registered as a CLI command in app/__init__.py)
       python scripts/seed_dev.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.extensions import db
from app.models.question import Question
from app.models.user import User
from app.services.question_service import normalize_answer, statement_hash

SAMPLE_QUESTIONS = [
    # level 1 - Rookie
    (1, "What is 7 + 5?", "12", "numeric", None, "Arithmetic"),
    (1, "What is 9 * 6?", "54", "numeric", None, "Arithmetic"),
    (1, "Simplify: 3/6", "1/2", "exact", None, "Fractions"),
    (1, "What is 15 - 8?", "7", "numeric", None, "Arithmetic"),
    (1, "What is the perimeter of a square with side 4?", "16", "numeric", None, "Geometry"),
    (1, "Solve for x: x + 3 = 10", "7", "numeric", None, "Algebra"),
    # level 2 - Genius
    (2, "Solve for x: 2x + 5 = 17", "6", "numeric", None, "Algebra"),
    (2, "What is the area of a circle with radius 3? (use pi ~= 3.14, round to nearest whole number)", "28", "numeric", None, "Geometry"),
    (2, "Simplify: (x^2 * x^3)", "x^5", "exact", None, "Algebra"),
    (2, "What is 20% of 150?", "30", "numeric", None, "Percentages"),
    (2, "Factor: x^2 - 9", "(x-3)(x+3)", "exact", None, "Algebra"),
    (2, "What is the slope of the line y = 3x + 2?", "3", "numeric", None, "Algebra"),
    # level 3 - Phantom
    (3, "Solve: x^2 - 5x + 6 = 0 (smaller root)", "2", "numeric", None, "Algebra"),
    (3, "What is the derivative of x^3?", "3x^2", "exact", None, "Calculus"),
    (3, "In a right triangle with legs 3 and 4, what is the hypotenuse?", "5", "numeric", None, "Geometry"),
    (3, "Evaluate: log_2(8)", "3", "numeric", None, "Logarithms"),
    (3, "What is the sum of the interior angles of a hexagon (degrees)?", "720", "numeric", None, "Geometry"),
    (3, "Simplify: sqrt(50)", "5*sqrt(2)", "exact", None, "Radicals"),
    # level 4 - Titan
    (4, "What is the integral of 2x dx?", "x^2 + C", "exact", None, "Calculus"),
    (4, "Solve: sin(x) = 0.5 for 0 <= x <= 90 (degrees)", "30", "numeric", None, "Trigonometry"),
    (4, "What is the determinant of [[2,3],[1,4]]?", "5", "numeric", None, "Linear Algebra"),
    (4, "Find the limit as x -> 0 of sin(x)/x", "1", "numeric", None, "Calculus"),
    (4, "How many ways can 5 distinct books be arranged on a shelf?", "120", "numeric", None, "Combinatorics"),
    (4, "What is the 5th term of the sequence a_n = 2n + 1?", "11", "numeric", None, "Sequences"),
    # level 5 - Omniscient
    (5, "Evaluate: the sum from n=1 to infinity of (1/2)^n", "1", "numeric", None, "Series"),
    (5, "What is the eigenvalue of [[2,0],[0,3]] with the larger magnitude?", "3", "numeric", None, "Linear Algebra"),
    (5, "Solve the differential equation dy/dx = y, y(0) = 1, at x = 0 (value of y)", "1", "numeric", None, "Differential Equations"),
    (5, "What is the number of Sylow 2-subgroups possible in a group of order 12 (list the standard answer: 1 or 3)?", "1", "exact", None, "Group Theory"),
    (5, "Evaluate: the definite integral of x^2 from 0 to 3", "9", "numeric", None, "Calculus"),
    (5, "What is the rank of the identity matrix I_4?", "4", "numeric", None, "Linear Algebra"),
]


def run():
    created = 0
    for level, statement, answer, answer_type, choices, topic in SAMPLE_QUESTIONS:
        s_hash = statement_hash(statement)
        if Question.query.filter_by(statement_hash=s_hash).first():
            continue
        q = Question(
            statement_hash=s_hash,
            level=level,
            statement=statement,
            statement_latex=statement,
            answer=normalize_answer(answer) if answer_type != "mcq" else answer,
            answer_type=answer_type,
            choices_json=None,
            topic=topic,
            source="seed_dev",
        )
        db.session.add(q)
        created += 1
    db.session.commit()
    print(f"Seeded {created} new questions ({Question.query.count()} total).")

    if not User.query.filter_by(username="demo").first():
        demo = User(username="demo", display_name="Demo User")
        demo.email = "demo@mathverse.local"
        demo.set_password("password123")
        db.session.add(demo)
        db.session.commit()
        print("Created demo user: demo@mathverse.local / password123")


if __name__ == "__main__":
    from app import create_app

    app = create_app()
    with app.app_context():
        run()
