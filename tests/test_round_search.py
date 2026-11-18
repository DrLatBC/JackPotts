"""Round search: the round's rules apply to every hand search the bot's decisions make."""

from __future__ import annotations

import ast
from pathlib import Path

from balatro_bot.context import RoundContext
from balatro_bot.domain.policy.discard_policy import _expected_play_value
from balatro_bot.domain.policy.hand_sequencing import _find_card_sharp_type
from balatro_bot.domain.policy.round_search import RoundSearch
from balatro_bot.strategy import compute_strategy
from tests.conftest import card, joker

SRC = Path(__file__).resolve().parent.parent / "src" / "balatro_bot"
SEARCH_FUNCTIONS = {"best_hand", "enumerate_hands"}


def _imports_search(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "balatro_bot.domain.scoring.search":
            if SEARCH_FUNCTIONS & {alias.name for alias in node.names}:
                return True
    return False


def test_decisions_search_through_the_round_search():
    decision_files = [*(SRC / "domain" / "policy").glob("*.py"), *(SRC / "rules").glob("*.py")]
    offenders = sorted(
        str(p.relative_to(SRC)) for p in decision_files
        if p.name != "round_search.py" and _imports_search(p)
    )
    assert offenders == []


HAND = [card("K", "H"), card("K", "D"), card("9", "C"), card("7", "S"), card("2", "H")]


def test_forced_card_follows_the_cards_searched():
    forced = HAND[2]
    search = RoundSearch(hand_cards=HAND, hand_levels={}, jokers=[], forced_card=forced)

    assert 2 in search.best().card_indices
    # Elsewhere in the list, the forced card is still required at its new position.
    moved = [HAND[2], HAND[0], HAND[1]]
    assert 0 in search.best(moved).card_indices
    # Absent from the list, nothing is required.
    assert search.best(HAND[:2]).hand_name == "Pair"


def test_exclude_adds_to_the_eye():
    search = RoundSearch(hand_cards=HAND, hand_levels={}, jokers=[], eye_used={"Pair"})
    assert search.best().hand_name == "High Card"
    assert search.best(exclude={"High Card"}) is None


def test_ignore_mouth_lifts_only_the_lock():
    search = RoundSearch(hand_cards=HAND, hand_levels={}, jokers=[], mouth_hand="Flush",
                         forced_card=HAND[2])
    assert search.best() is None
    relaxed = search.best(ignore_mouth=True)
    assert relaxed is not None and 2 in relaxed.card_indices


# --- Every decision's search applies the round's rules ---------------------------

def _ctx(hand=HAND, jokers=(), deck=None, **fields):
    ctx = RoundContext(
        blind_score=5000, blind_name=fields.pop("blind_name", "Small Blind"), chips_scored=0,
        chips_remaining=5000, hands_left=fields.pop("hands_left", 4),
        discards_left=fields.pop("discards_left", 3),
        hand_cards=list(hand), hand_levels={}, jokers=list(jokers), best=None,
        money=5, ante=1, round_num=1, min_cards=1,
        strategy=compute_strategy(list(jokers), {}), deck_cards=deck or [], **fields,
    )
    ctx.best = ctx.search.best()
    return ctx


def test_stencil_counts_open_slots_in_every_search():
    ctx = _ctx(jokers=[joker("j_stencil")], joker_limit=3)
    alone = _ctx(jokers=[joker("j_stencil")], joker_limit=1)
    # Three empty slots counting the Stencil itself: X3.
    assert ctx.best.total == 3 * alone.best.total
    assert ctx.search.enumerate()[0].total == ctx.best.total


def test_card_sharp_never_targets_a_hand_the_eye_has_used():
    ctx = _ctx(jokers=[joker("j_card_sharp")], blind_name="The Eye", eye_used_hands={"Pair"})
    assert _find_card_sharp_type(ctx) != "Pair"


def test_discard_ev_scores_nothing_the_mouth_forbids():
    # Keeping two different suits can never make the locked Flush.
    deck = [card(r, s) for s in ("C", "S") for r in ("3", "4", "5", "6", "8", "T", "J", "Q", "A")]
    locked = _ctx(deck=deck, blind_name="The Mouth", mouth_locked_hand="Flush")
    free = _ctx(deck=deck)
    assert _expected_play_value([0, 1], locked) == 0
    assert _expected_play_value([0, 1], free) > 0


def test_forced_card_stays_in_the_fallback_searches():
    ctx = _ctx(forced_card_idx=2, mouth_locked_hand="Flush")
    assert 2 in ctx.search.best(ignore_mouth=True).card_indices
    assert 2 in ctx.search.best(ignore_mouth=True, exclude={"Pair"}).card_indices


def test_the_idol_scores_in_the_finisher():
    with_idol = _ctx(jokers=[joker("j_idol")], idol_rank="K", idol_suit="H")
    without = _ctx(jokers=[joker("j_idol")])
    assert with_idol.search.best(hands_left=1).total > without.search.best(hands_left=1).total
