"""Typed game-state snapshots — immutable read models from the API."""

from __future__ import annotations

from dataclasses import dataclass

from balatro_bot.domain.models.card import Card
from balatro_bot.domain.models.joker import Joker


@dataclass(frozen=True)
class BlindSnapshot:
    key: str
    name: str
    score: int
    status: str
    boss_disabled: bool = False


@dataclass(frozen=True)
class RoundSnapshot:
    chips: int
    hands_left: int
    discards_left: int
    ancient_suit: str | None
    most_played_poker_hand: str | None = None


@dataclass(frozen=True)
class Snapshot:
    state_name: str
    seed: str
    ante: int
    round_num: int
    money: int
    joker_limit: int
    deck_count: int
    round: RoundSnapshot
    current_blind: BlindSnapshot
    hand_cards: list[Card]
    hand_levels: dict[str, dict]
    jokers: list[Joker]
    deck_cards: list[Card]
    consumables: list[Card]
    shop_cards: list[Card]
    vouchers: list[Card]
    pack_cards: list[Card]
