"""Hand scoring: chips, mult and total for one played hand, from the compiled kernel."""

from __future__ import annotations

from balatro_bot.cards import card_chip_value, card_mult_value, card_xmult_value
from balatro_bot.constants import HAND_INFO
from balatro_bot.domain.models.card import Card
from balatro_bot.domain.scoring.kernel import KernelScorer
from balatro_bot.joker_effects.roster import Roster


def score_hand(
    hand_name: str,
    scoring_cards: list[dict],
    hand_levels: dict[str, dict] | None = None,
    jokers: list[dict] | None = None,
    played_cards: list[dict] | None = None,
    held_cards: list[dict] | None = None,
    money: int = 0,
    discards_left: int = 0,
    hands_left: int = 1,
    joker_limit: int = 5,
    ancient_suit: str | None = None,
    deck_count: int = 0,
    blind_name: str = "",
    ox_most_played: str | None = None,
    idol_rank: str | None = None,
    idol_suit: str | None = None,
    rng=None,
    lineup: Roster | None = None,
) -> tuple[int, int, int]:
    """Compute (chips, mult, total) for a hand.

    *lineup* is ``roster(jokers)`` when the caller already has it.
    """
    return KernelScorer(
        hand_levels, jokers, money=money, discards_left=discards_left, hands_left=hands_left,
        joker_limit=joker_limit, ancient_suit=ancient_suit, deck_count=deck_count,
        blind_name=blind_name, ox_most_played=ox_most_played,
        idol_rank=idol_rank, idol_suit=idol_suit, lineup=lineup,
    ).score(hand_name, scoring_cards, played_cards, held_cards, rng)


def score_hand_detailed(
    hand_name: str,
    scoring_cards: list[dict],
    hand_levels: dict[str, dict] | None = None,
    jokers: list[dict] | None = None,
    played_cards: list[dict] | None = None,
    held_cards: list[dict] | None = None,
    money: int = 0,
    discards_left: int = 0,
    hands_left: int = 1,
    joker_limit: int = 5,
    ancient_suit: str | None = None,
    deck_count: int = 0,
    blind_name: str = "",
    ox_most_played: str | None = None,
    idol_rank: str | None = None,
    idol_suit: str | None = None,
) -> dict:
    """Like score_hand but returns a full breakdown dict for logging."""
    base_chips, base_mult, _ = HAND_INFO[hand_name]
    if hand_levels and hand_name in hand_levels:
        lvl = hand_levels[hand_name]
        base_chips = lvl.get("chips", base_chips)
        base_mult = lvl.get("mult", base_mult)

    card_details = []
    for c in scoring_cards:
        if isinstance(c, Card):
            label = c.label or "?"
            mod = c.modifier
            if mod.edition or mod.enhancement:
                label += f"[{mod.edition or ''}/{mod.enhancement or ''}]"
        else:
            label = c.get("label", "?")
            mod = c.get("modifier", {})
            if isinstance(mod, dict) and (mod.get("edition") or mod.get("enhancement")):
                label += f"[{mod.get('edition','')}/{mod.get('enhancement','')}]"
        card_details.append((label, card_chip_value(c), card_mult_value(c), card_xmult_value(c)))

    return {
        "hand_name": hand_name,
        "base_chips": base_chips, "base_mult": base_mult,
        "card_details": card_details,
        **KernelScorer(
            hand_levels, jokers, money=money, discards_left=discards_left, hands_left=hands_left,
            joker_limit=joker_limit, ancient_suit=ancient_suit, deck_count=deck_count,
            blind_name=blind_name, ox_most_played=ox_most_played,
            idol_rank=idol_rank, idol_suit=idol_suit,
        ).breakdown(hand_name, scoring_cards, played_cards, held_cards),
    }
