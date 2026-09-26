import pytest

from app.services import elo_service
from app.tiers import ELO_FLOOR, TIERS, tier_for_elo


def test_expected_score_symmetric_at_equal_elo():
    assert elo_service.expected_score(1000, 1000) == pytest.approx(0.5)


def test_expected_score_favors_higher_elo():
    assert elo_service.expected_score(1400, 1000) > 0.5
    assert elo_service.expected_score(1000, 1400) < 0.5


def test_win_delta_within_tier_band():
    tier = tier_for_elo(1000)  # Genius: win 20-35
    for opp in (600, 1000, 1600):
        delta = elo_service.compute_win_delta(tier, elo_service.expected_score(1000, opp))
        assert tier.win_min <= delta <= tier.win_max


def test_win_delta_pays_more_for_upset():
    tier = tier_for_elo(1000)
    beating_weaker = elo_service.compute_win_delta(tier, elo_service.expected_score(1000, 600))
    beating_stronger = elo_service.compute_win_delta(tier, elo_service.expected_score(1000, 1600))
    assert beating_stronger > beating_weaker


def test_loss_delta_within_tier_band_and_hurts_more_for_downset():
    tier = tier_for_elo(1000)
    losing_to_stronger = elo_service.compute_loss_delta(tier, elo_service.expected_score(1000, 1600))
    losing_to_weaker = elo_service.compute_loss_delta(tier, elo_service.expected_score(1000, 600))
    assert tier.loss_min <= losing_to_stronger <= tier.loss_max
    assert tier.loss_min <= losing_to_weaker <= tier.loss_max
    assert losing_to_weaker > losing_to_stronger


@pytest.mark.parametrize("elo,expected_tier", [(0, "Rookie"), (799, "Rookie"), (800, "Genius"),
                                                (1199, "Genius"), (1200, "Phantom"), (1799, "Phantom"),
                                                (1800, "Titan"), (2199, "Titan"), (2200, "Omniscient"),
                                                (5000, "Omniscient")])
def test_tier_boundaries(elo, expected_tier):
    assert tier_for_elo(elo).name == expected_tier


def test_apply_match_result_win():
    result = elo_service.apply_match_result(player_elo=1000, opponent_elo=1000, outcome="win")
    tier = tier_for_elo(1000)
    assert tier.win_min <= result.delta <= tier.win_max
    assert result.new_elo == 1000 + result.delta


def test_apply_match_result_loss():
    result = elo_service.apply_match_result(player_elo=1000, opponent_elo=1000, outcome="loss")
    tier = tier_for_elo(1000)
    assert -tier.loss_max <= result.delta <= -tier.loss_min
    assert result.new_elo == 1000 + result.delta


def test_apply_match_result_draw_near_zero_for_even_match():
    result = elo_service.apply_match_result(player_elo=1000, opponent_elo=1000, outcome="draw")
    assert abs(result.delta) <= 2


def test_provisional_multiplier_scales_delta():
    normal = elo_service.apply_match_result(player_elo=1000, opponent_elo=1000, outcome="win")
    provisional = elo_service.apply_match_result(
        player_elo=1000, opponent_elo=1000, outcome="win", is_provisional=True
    )
    assert provisional.delta == round(normal.delta * elo_service.PLACEMENT_MULTIPLIER)


def test_elo_floor_is_never_breached():
    result = elo_service.apply_match_result(
        player_elo=ELO_FLOOR, opponent_elo=2000, outcome="loss"
    )
    assert result.new_elo >= ELO_FLOOR


def test_shield_negates_first_floor_loss():
    tier = tier_for_elo(800)  # Genius floor
    result = elo_service.apply_match_result(
        player_elo=tier.min_elo,
        opponent_elo=tier.min_elo,
        outcome="loss",
        at_floor=True,
        shield_available=True,
    )
    assert result.delta == 0
    assert result.new_elo == tier.min_elo
    assert result.shield_used is True


def test_shield_not_used_when_unavailable():
    tier = tier_for_elo(800)
    result = elo_service.apply_match_result(
        player_elo=tier.min_elo,
        opponent_elo=tier.min_elo,
        outcome="loss",
        at_floor=True,
        shield_available=False,
    )
    assert result.delta < 0
    assert result.shield_used is False


def test_is_at_tier_floor():
    assert elo_service.is_at_tier_floor(800) is True
    assert elo_service.is_at_tier_floor(801) is False


@pytest.mark.parametrize("correct,expected_min_relation", [(5, "plus150"), (4, "floor"), (3, "floor"), (2, "below"), (0, "below")])
def test_placement_seed(correct, expected_min_relation):
    tier = tier_for_elo(800)  # Genius
    seed = elo_service.placement_seed(tier, correct)
    if expected_min_relation == "plus150":
        assert seed == tier.min_elo + 150
    elif expected_min_relation == "floor":
        assert seed == tier.min_elo
    else:
        assert seed == max(400, TIERS[TIERS.index(tier) - 1].min_elo)


def test_placement_seed_never_below_400():
    tier = tier_for_elo(400)  # Rookie
    seed = elo_service.placement_seed(tier, 0)
    assert seed >= 400


def test_resolve_tie_break_by_score():
    assert elo_service.resolve_tie_break(10, 5, 0, 0, 0, 0) == "a"
    assert elo_service.resolve_tie_break(5, 10, 0, 0, 0, 0) == "b"


def test_resolve_tie_break_by_wrong_answers():
    assert elo_service.resolve_tie_break(10, 10, 1, 3, 0, 0) == "a"


def test_resolve_tie_break_by_time():
    assert elo_service.resolve_tie_break(10, 10, 2, 2, 5000, 8000) == "a"


def test_resolve_tie_break_draw():
    assert elo_service.resolve_tie_break(10, 10, 2, 2, 5000, 5000) == "draw"
