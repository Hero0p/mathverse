"""Elo rating math for the Arena.

Pure functions, no DB/Flask access, so they're trivially unit-testable.
Tier bands (win/loss ranges, seed elo, floor) all come from app.tiers.TIERS --
nothing here hardcodes a number that belongs in that table.
"""
from dataclasses import dataclass

from app.tiers import ELO_FLOOR, Tier, tier_below, tier_for_elo

PLACEMENT_MULTIPLIER = 1.5
PLACEMENT_MATCH_COUNT = 5


@dataclass
class EloResult:
    delta: int
    new_elo: int
    shield_used: bool = False


def expected_score(elo_a: int, elo_b: int) -> float:
    """Probability that player A beats player B, standard logistic Elo formula."""
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def _clamp(value: float, lo: float, hi: float) -> int:
    return int(round(max(lo, min(hi, value))))


def compute_win_delta(tier: Tier, expected: float) -> int:
    """Winner's gain: beating a stronger opponent (low `expected`) pays more."""
    raw = tier.win_min + (tier.win_max - tier.win_min) * (1 - expected)
    return _clamp(raw, tier.win_min, tier.win_max)


def compute_loss_delta(tier: Tier, expected: float) -> int:
    """Loser's cost: losing to a weaker opponent (high `expected`) hurts more.
    Returned as a positive magnitude to be subtracted by the caller."""
    raw = tier.loss_min + (tier.loss_max - tier.loss_min) * expected
    return _clamp(raw, tier.loss_min, tier.loss_max)


def compute_draw_delta(tier: Tier, expected: float) -> int:
    """Signed delta for a draw: positive if the player was expected to lose,
    negative if they were expected to win, ~0 for an even match."""
    raw = (tier.win_max - tier.loss_max) / 4.0 * (0.5 - expected) * 2.0
    return int(round(raw))


def apply_match_result(
    *,
    player_elo: int,
    opponent_elo: int,
    outcome: str,  # 'win' | 'loss' | 'draw'
    is_provisional: bool = False,
    at_floor: bool = False,
    shield_available: bool = False,
) -> EloResult:
    """Compute the signed Elo delta and resulting rating for one player.

    Bands are read from the PLAYER's own current tier (per spec 7.3), not the
    opponent's, so a Rookie beating a Titan still gains Rookie-band Elo.
    """
    tier = tier_for_elo(player_elo)
    expected = expected_score(player_elo, opponent_elo)

    if outcome == "win":
        delta = compute_win_delta(tier, expected)
    elif outcome == "loss":
        delta = -compute_loss_delta(tier, expected)
    elif outcome == "draw":
        delta = compute_draw_delta(tier, expected)
    else:
        raise ValueError(f"Unknown outcome: {outcome}")

    if is_provisional:
        delta = int(round(delta * PLACEMENT_MULTIPLIER))

    shield_used = False
    if outcome == "loss" and at_floor and shield_available:
        delta = 0
        shield_used = True

    new_elo = max(ELO_FLOOR, player_elo + delta)
    # Recompute the effective delta after flooring, so the audit trail is honest.
    effective_delta = new_elo - player_elo
    return EloResult(delta=effective_delta, new_elo=new_elo, shield_used=shield_used)


def is_at_tier_floor(elo: int) -> bool:
    tier = tier_for_elo(elo)
    return elo == tier.min_elo


def placement_seed(tier: Tier, correct_count: int) -> int:
    """Seed Elo from a 5-question placement quiz at `tier`'s level.

    5/5 -> tier floor + 150. 3-4 -> tier floor. 0-2 -> the tier below's floor,
    never lower than 400 so a bad placement run doesn't crater a new player.
    """
    if correct_count >= 5:
        return tier.min_elo + 150
    if correct_count >= 3:
        return tier.min_elo
    lower = tier_below(tier)
    return max(lower.min_elo, 400)


def resolve_tie_break(
    score_a: int, score_b: int, wrong_a: int, wrong_b: int, time_ms_a: int, time_ms_b: int
) -> str:
    """Return 'a', 'b', or 'draw' using the spec's ordered tie-break rules."""
    if score_a != score_b:
        return "a" if score_a > score_b else "b"
    if wrong_a != wrong_b:
        return "a" if wrong_a < wrong_b else "b"
    if time_ms_a != time_ms_b:
        return "a" if time_ms_a < time_ms_b else "b"
    return "draw"
