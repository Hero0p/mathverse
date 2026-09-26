import pytest

from app.services.question_service import (
    answers_match,
    grade_answer,
    normalize_answer,
    normalize_statement,
    statement_hash,
)


@pytest.mark.parametrize("raw,expected", [
    ("1/2", "1/2"),
    ("2/4", "1/2"),
    ("4/2", "2"),
    ("0.5", "1/2"),
    ("0.50", "1/2"),
    ("5", "5"),
    ("5.0", "5"),
    (" 5 ", "5"),
    ("$5$", "5"),
    ("-3/6", "-1/2"),
])
def test_normalize_answer_equivalent_forms(raw, expected):
    assert normalize_answer(raw) == expected


def test_normalize_answer_case_insensitive_for_text():
    assert normalize_answer("Yes") == normalize_answer("yes")


def test_normalize_answer_empty_and_none():
    assert normalize_answer(None) == ""
    assert normalize_answer("") == ""


def test_answers_match_equivalent_fractions():
    assert answers_match("1/2", "2/4") is True
    assert answers_match("0.5", "1/2") is True
    assert answers_match("1/3", "1/2") is False


def test_normalize_statement_collapses_whitespace_and_case():
    assert normalize_statement("  What   is 2+2?  ") == normalize_statement("what is 2+2?")


def test_statement_hash_stable_across_whitespace_variants():
    assert statement_hash("What is 2+2?") == statement_hash("  what   is 2+2?  ")


def test_statement_hash_differs_for_different_statements():
    assert statement_hash("What is 2+2?") != statement_hash("What is 3+3?")


class _FakeQuestion:
    def __init__(self, answer, answer_type="exact"):
        self.answer = answer
        self.answer_type = answer_type


def test_grade_answer_numeric_equivalent():
    q = _FakeQuestion("0.5", answer_type="numeric")
    assert grade_answer(q, "1/2") is True
    assert grade_answer(q, "0.50") is True
    assert grade_answer(q, "0.6") is False


def test_grade_answer_mcq_matches_exact_choice_text():
    q = _FakeQuestion("Paris", answer_type="mcq")
    assert grade_answer(q, "Paris") is True
    assert grade_answer(q, "paris") is True
    assert grade_answer(q, "London") is False


def test_grade_answer_none_given_is_never_correct():
    q = _FakeQuestion("5")
    assert grade_answer(q, None) is False
