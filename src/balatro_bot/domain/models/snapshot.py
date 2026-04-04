"""Typed game-state snapshots — immutable read models from the API."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BlindSnapshot:
    key: str
    name: str
    score: int
    status: str


@dataclass(frozen=True)
class RoundSnapshot:
    chips: int
    hands_left: int
    discards_left: int
    ancient_suit: str | None


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
    # Raw collections — typed card models deferred to Phase 2
    hand_cards: list[dict]
    hand_levels: dict[str, dict]
    jokers: list[dict]
    deck_cards: list[dict]
    consumables: list[dict]
    shop_cards: list[dict]
    vouchers: list[dict]
    pack_cards: list[dict]
