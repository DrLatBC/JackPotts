"""The scoring kernel agrees with itself and with its saved answers on generated cases."""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from balatro_bot.domain.scoring.kernel import joker_phases, prepare_play
from balatro_bot.domain.scoring.play_order import sort_play_order
from balatro_bot.domain.scoring.search import enumerate_hands
from tests.kernel_cases import (
    SCORING_KEYS, kernel_answer, kernel_scorer, materialise, pair_case, random_case, saved_cases,
    score_results, search_result, targeted_case,
)

HERE = Path(__file__).parent

SEEDS = range(150)


def test_a_seed_always_builds_the_same_case():
    for seed in (0, 7, 123):
        assert random_case(seed) == random_case(seed)
        assert search_result(random_case(seed), typed=True) == search_result(random_case(seed), typed=True)


@pytest.mark.parametrize("typed", [True, False])
def test_best_hand_is_the_first_enumerated_hand(typed):
    for seed in SEEDS:
        every, best = search_result(random_case(seed), typed)
        assert best == (every[0] if every else None), seed


def test_typed_and_raw_inputs_search_the_same():
    for seed in SEEDS:
        case = random_case(seed)
        assert search_result(case, typed=True) == search_result(case, typed=False), seed


def test_typed_and_raw_inputs_score_the_same():
    for seed in SEEDS:
        case = random_case(seed)
        assert score_results(case, typed=True) == score_results(case, typed=False), seed


def test_scoring_one_play_twice_gives_the_same_numbers():
    for seed in SEEDS:
        case = random_case(seed)
        assert score_results(case, typed=True) == score_results(case, typed=True), seed


def test_joker_pairs_search_the_same_typed_and_raw():
    keys = SCORING_KEYS[::12]
    for first in keys:
        for second in keys:
            for variant in range(5):
                case = pair_case(first, second, variant)
                assert search_result(case, typed=True) == search_result(case, typed=False), (first, second, variant)


def test_answers_match_the_saved_ones():
    """tests/kernel_expected.json holds answers the kernel and the Python scorer agreed
    on, before the Python scorer was removed."""
    saved = json.loads((HERE / "kernel_expected.json").read_text(encoding="utf-8"))["cases"]
    cases = list(saved_cases())
    assert [name for name, _ in cases] == [entry["name"] for entry in saved]
    for (name, case), expected in zip(cases, saved):
        assert kernel_answer(name, case, typed=False) == expected, name
        if name.startswith("random"):
            assert kernel_answer(name, case, typed=True) == expected, name


def test_joker_phases_match_the_saved_ones():
    """Each joker's ordering phase and whether the shop's sim scores it, as they were
    when the Python effect tables answered both."""
    from balatro_bot.domain.policy.shop_valuation import _has_scoring_effect
    from balatro_bot.joker_effects.scoring_phase import get_joker_phase

    saved = json.loads((HERE / "joker_phase_expected.json").read_text(encoding="utf-8"))
    assert set(joker_phases()) <= set(saved)
    assert {key: [get_joker_phase(key), _has_scoring_effect(key)] for key in saved} == saved


def test_the_largest_hand_is_searched():
    case = random_case(1, [32])
    hand, jokers = materialise(case, typed=True)
    scorer = kernel_scorer(case, jokers)
    found = scorer.best(hand)
    assert found is not None and max(found[1]) < 32
    assert scorer.best_totals([hand]) == [found[4]]


def test_a_hand_past_the_limit_is_refused():
    case = random_case(3, [33])
    hand, jokers = materialise(case, typed=True)
    with pytest.raises(ValueError, match="too many cards"):
        kernel_scorer(case, jokers).best(hand)


def test_batch_calls_agree_with_single_calls():
    """Scoring many plays or searching many hands in one call gives the one-at-a-time answers."""
    for seed in range(80):
        case = random_case(seed)
        hand, jokers = materialise(case, typed=True)
        scorer = kernel_scorer(case, jokers)

        hands = [hand[:size] for size in range(1, len(hand) + 1)]
        singly = [scorer.best(h) for h in hands]
        assert scorer.best_totals(hands) == [0 if found is None else found[4] for found in singly], seed

        plays = []
        for name, indices, _chips, _mult, _total, scoring in scorer.search(hand)[:12]:
            played = [hand[i] for i in indices]
            held = [c for i, c in enumerate(hand) if i not in indices]
            plays.append((name, [hand[i] for i in scoring], played, held))
        prepared = [prepare_play(*play) for play in plays]
        rolls = lambda: [random.Random(seed * 100 + i) for i in range(len(plays))]  # noqa: E731
        many = scorer.score_many(prepared, rolls())
        for (chips, mult), play, rng in zip(many, plays, rolls()):
            assert (chips, mult) == scorer.score(*play, rng=rng)[:2], seed


def test_kernel_play_order_matches_the_play_orderer():
    """The kernel's play order and PlayOrderer's agree for every joker lineup."""
    cases = [random_case(seed) for seed in range(150)]
    cases += [targeted_case(key, variant) for key in SCORING_KEYS for variant in range(3)]
    for case in cases:
        hand, jokers = materialise(case, typed=False)
        if not jokers:
            continue
        scorer = kernel_scorer(case, jokers)
        settings = {k: case.search.get(k) for k in ("idol_rank", "idol_suit", "ancient_suit")}
        for candidate in enumerate_hands(hand, case.levels, jokers=jokers, **case.search)[:20]:
            indices = list(candidate.card_indices)
            assert scorer.play_order(hand, indices) == sort_play_order(indices, hand, jokers, **settings), (
                case.seed, indices)
