"""Answer normalization and grading, shared by ingestion and live grading
so a question is graded the exact same way it was validated at import time."""
import hashlib
import re
from fractions import Fraction


def normalize_statement(statement: str) -> str:
    """Collapse whitespace/case for stable dedup hashing."""
    return re.sub(r"\s+", " ", statement.strip().lower())


def statement_hash(statement: str) -> str:
    return hashlib.sha256(normalize_statement(statement).encode("utf-8")).hexdigest()


def normalize_answer(raw: str) -> str:
    """Canonicalize an answer string so equivalent forms compare equal:
    whitespace, case, surrounding $ / \\( \\) latex delimiters, fractions
    (1/2 == 2/4), decimals (0.50 == 0.5 == 1/2), and +/- sign spacing.

    Fractions and decimals are both routed through `Fraction`, which parses
    both "1/2" and "0.5" style strings, so any numerically-equal answer
    normalizes to the same canonical rational representation regardless of
    which form the student (or the dataset) used.
    """
    if raw is None:
        return ""
    s = raw.strip()
    s = re.sub(r"^\$+|\$+$", "", s).strip()
    s = re.sub(r"^\\\(|\\\)$", "", s).strip()
    s = s.replace(" ", "")
    s = s.rstrip(".")
    if not s:
        return ""

    try:
        frac = Fraction(s)
        if frac.denominator == 1:
            return str(frac.numerator)
        return f"{frac.numerator}/{frac.denominator}"
    except (ValueError, ZeroDivisionError):
        pass

    return s.lower()


def answers_match(given: str, expected: str) -> bool:
    return normalize_answer(given) == normalize_answer(expected)


def grade_answer(question, given: str) -> bool:
    """Grade `given` against `question.answer`. MCQ compares choice text or
    index; numeric/exact compare via normalize_answer."""
    if given is None:
        return False
    if question.answer_type == "mcq":
        return answers_match(given, question.answer) or given.strip() == question.answer.strip()
    return answers_match(given, question.answer)
