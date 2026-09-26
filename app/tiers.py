"""Single source of truth for Arena tier bands.

Every place that needs a tier's Elo range, question level, or Elo movement
bounds reads it from this module -- never hardcode tier numbers elsewhere.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    name: str
    min_elo: int
    max_elo: float  # float("inf") for the open-ended top tier
    level: int  # Question.level served to players in this tier
    win_min: int
    win_max: int
    loss_min: int
    loss_max: int
    seed_elo: int  # Elo assigned when a player picks this tier at placement


TIERS: list[Tier] = [
    Tier("Rookie", 0, 799, 1, 30, 40, 10, 15, seed_elo=400),
    Tier("Genius", 800, 1199, 2, 20, 35, 15, 20, seed_elo=800),
    Tier("Phantom", 1200, 1799, 3, 12, 20, 18, 24, seed_elo=1200),
    Tier("Titan", 1800, 2199, 4, 8, 12, 15, 18, seed_elo=1800),
    Tier("Omniscient", 2200, float("inf"), 5, 8, 12, 15, 18, seed_elo=2200),
]

ELO_FLOOR = 100
PLACEMENT_TIER_NAMES = ["Rookie", "Genius", "Phantom", "Titan"]  # choosable starting points


def tier_for_elo(elo: int | None) -> Tier:
    """Return the Tier band containing `elo`. Unrated (None) players are Rookie."""
    if elo is None:
        elo = 0
    for tier in TIERS:
        if tier.min_elo <= elo <= tier.max_elo:
            return tier
    return TIERS[-1] if elo > TIERS[-1].min_elo else TIERS[0]


def tier_by_name(name: str) -> Tier:
    for tier in TIERS:
        if tier.name.lower() == name.lower():
            return tier
    raise ValueError(f"Unknown tier: {name}")


def tier_index(tier: Tier) -> int:
    return TIERS.index(tier)


def tier_below(tier: Tier) -> Tier:
    idx = tier_index(tier)
    return TIERS[max(0, idx - 1)]
