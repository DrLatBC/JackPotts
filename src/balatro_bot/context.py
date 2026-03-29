"""RoundContext — pre-computed facts about the current round for rules to use."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from balatro_bot.hand_evaluator import HandCandidate, best_hand
from balatro_bot.strategy import Strategy, compute_strategy

if TYPE_CHECKING:
    from typing import Any


@dataclass
class RoundContext:
    blind_score: int
    blind_name: str
    chips_scored: int
    chips_remaining: int
    hands_left: int
    discards_left: int
    hand_cards: list[dict]
    hand_levels: dict[str, dict]
    jokers: list[dict]
    best: HandCandidate | None
    money: int
    ante: int
    round_num: int
    min_cards: int
    strategy: Strategy
    deck_cards: list[dict]
    mouth_locked_hand: str | None = None
    score_discount: float = 1.0
    forced_card_idx: int | None = None
    ancient_suit: str | None = None

    @staticmethod
    def from_state(state: dict[str, Any]) -> RoundContext:
        cached = state.get("_round_ctx")
        if cached is not None:
            return cached

        hand_cards = state.get("hand", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        rnd = state.get("round", {})
        jokers = state.get("jokers", {}).get("cards", [])
        money = state.get("money", 0)
        deck_cards = state.get("cards", {}).get("cards", [])

        blind_score = 0
        blind_name = ""
        for b in state.get("blinds", {}).values():
            if isinstance(b, dict) and b.get("status") == "CURRENT":
                blind_score = b.get("score", 0)
                blind_name = b.get("name", "")
                break

        min_cards = 5 if blind_name == "The Psychic" else 1

        mouth_locked_hand = None
        if blind_name == "The Mouth":
            for hand_name, hand_data in hand_levels.items():
                if isinstance(hand_data, dict) and hand_data.get("played_this_round", 0) > 0:
                    mouth_locked_hand = hand_name
                    break

        chips_scored = rnd.get("chips", 0)
        hands_left = rnd.get("hands_left", 0)
        discards_left = rnd.get("discards_left", 0)

        joker_count = len(jokers)
        score_discount = (
            (joker_count - 1) / joker_count if blind_name == "Crimson Heart" and joker_count > 1
            else 0.5 if blind_name == "Crimson Heart"
            else 1.0
        )

        forced_card_idx = None
        if blind_name == "Cerulean Bell":
            for i, c in enumerate(hand_cards):
                s = c.get("state", {})
                if isinstance(s, dict) and s.get("highlight"):
                    forced_card_idx = i
                    break

        ancient_suit = rnd.get("ancient_suit")

        strat = compute_strategy(jokers, hand_levels)

        ctx = RoundContext(
            blind_score=blind_score,
            blind_name=blind_name,
            chips_scored=chips_scored,
            chips_remaining=blind_score - chips_scored,
            hands_left=hands_left,
            discards_left=discards_left,
            hand_cards=hand_cards,
            hand_levels=hand_levels,
            jokers=jokers,
            best=best_hand(
                hand_cards, hand_levels,
                min_select=min_cards, jokers=jokers,
                money=money, discards_left=discards_left,
                hands_left=hands_left,
                joker_limit=state.get("jokers", {}).get("limit", 5),
                required_hand=mouth_locked_hand,
                required_card_indices={forced_card_idx} if forced_card_idx is not None else None,
                ancient_suit=ancient_suit,
            ),
            mouth_locked_hand=mouth_locked_hand,
            score_discount=score_discount,
            forced_card_idx=forced_card_idx,
            ancient_suit=ancient_suit,
            money=money,
            ante=state.get("ante_num", 1),
            round_num=state.get("round_num", 1),
            min_cards=min_cards,
            strategy=strat,
            deck_cards=deck_cards,
        )
        state["_round_ctx"] = ctx
        return ctx
