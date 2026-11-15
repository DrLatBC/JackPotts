"""The scoring kernel agrees with itself on generated cases."""
from __future__ import annotations

import pytest

from tests.kernel_cases import SCORING_KEYS, pair_case, random_case, score_results, search_result

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
