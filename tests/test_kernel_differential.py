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


def test_hands_over_sixteen_cards_match():
    """Turtle Bean, Juggler and the hand-size vouchers take a hand past sixteen cards."""
    import random

    from tests.kernel_cases import _kernel_scorer, materialise

    sizes = [17, 17, 18, 19, 20, 22]
    high_position = 0
    for seed in range(16):
        case = random_case(seed, sizes)
        size = len(case.hand)
        if seed % 2:
            # The forced card sits past the sixteenth place.
            case.search["required_card_indices"] = {random.Random(seed).randrange(16, size)}
            high_position += 1
        typed = seed % 3 == 0
        assert kernel_search_result(case, typed) == search_result(case, typed), (seed, size)
        if seed < 6:
            for python, kernel in score_pairs(case, typed):
                assert kernel == python, (seed, size)
    assert high_position >= 8

    # The largest hand the kernel takes, and the discard sampling's batch call.
    case = random_case(1, [32])
    hand, jokers = materialise(case, typed=True)
    scorer = _kernel_scorer(case, jokers)
    found = scorer.best(hand)
    assert found is not None and max(found[1]) < 32
    assert scorer.best_totals([hand]) == [found[4]]


def test_a_hand_past_the_limit_is_refused():
    from tests.kernel_cases import _kernel_scorer, materialise

    case = random_case(3, [33])
    hand, jokers = materialise(case, typed=True)
    with pytest.raises(ValueError, match="too many cards"):
        _kernel_scorer(case, jokers).best(hand)


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


def test_batch_calls_agree_with_single_calls():
    """Scoring many plays or searching many hands in one call gives the one-at-a-time answers."""
    import random

    from balatro_bot.domain.scoring.kernel import prepare_play
    from tests.kernel_cases import _kernel_scorer, materialise

    for seed in range(80):
        case = random_case(seed)
        hand, jokers = materialise(case, typed=True)
        scorer = _kernel_scorer(case, jokers)

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
