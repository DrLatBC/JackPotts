"""The compiled kernel gives the Python kernel's answers, number for number."""
from __future__ import annotations

import pytest

pytest.importorskip("jackpotts_kernel", reason="the compiled kernel is not built (kernel/py)")

from tests.kernel_cases import (  # noqa: E402
    SCORING_KEYS, kernel_search_result, pair_case, random_case, score_pairs, search_result, targeted_case,
)

SEEDS = range(300)


@pytest.mark.parametrize("typed", [True, False])
def test_scoring_matches(typed):
    for seed in SEEDS:
        for python, kernel in score_pairs(random_case(seed), typed):
            assert kernel == python, seed


@pytest.mark.parametrize("typed", [True, False])
def test_search_matches(typed):
    for seed in SEEDS:
        case = random_case(seed)
        assert kernel_search_result(case, typed) == search_result(case, typed), seed


def test_joker_pairs_match():
    keys = SCORING_KEYS[::9]
    for first in keys:
        for second in keys:
            for variant in range(5):
                case = pair_case(first, second, variant)
                where = (first, second, variant)
                assert kernel_search_result(case, typed=True) == search_result(case, typed=True), where
                for python, kernel in score_pairs(case, typed=False):
                    assert kernel == python, where


def test_every_joker_matches_over_each_enhancement():
    for key in SCORING_KEYS:
        for variant in range(13):
            case = targeted_case(key, variant)
            where = (key, variant)
            assert kernel_search_result(case, typed=True) == search_result(case, typed=True), where
            for python, kernel in score_pairs(case, typed=False):
                assert kernel == python, where
