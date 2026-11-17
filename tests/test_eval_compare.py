"""Two models' evaluation games compared seed by seed."""

import pytest

from balatro_bot.eval_compare import compare, describe


def _g(seed, ante):
    return {"seed": seed, "won": ante == 9, "ante": ante}


def test_each_seed_gives_one_difference():
    new = [_g("a", 9), _g("b", 4), _g("c", 2), _g("d", 5)]
    old = [_g("a", 4), _g("b", 4), _g("c", 3), _g("d", 9)]
    c = compare(new, old)
    assert c["seeds"] == 4
    assert c["ante gain"] == pytest.approx((5 + 0 - 1 - 4) / 4)
    assert c["win gain"] == 0.0 and c["won only here"] == 1 and c["won only by base"] == 1
    assert c["win rate"] == c["base win rate"] == 0.25
    # Standard error of the per-seed ante differences 5, 0, -1, -4.
    diffs = [5, 0, -1, -4]
    mean = sum(diffs) / 4
    se = (sum((d - mean) ** 2 for d in diffs) / 3 / 4) ** 0.5
    assert c["ante gain +/-"] == pytest.approx(se)


def test_only_seeds_both_played_count():
    c = compare([_g("a", 5), _g("x", 9)], [_g("a", 3), _g("y", 9)])
    assert c["seeds"] == 1 and c["ante gain"] == 2 and c["ante gain +/-"] == 0.0
    assert compare([_g("x", 9)], [_g("y", 9)]) is None


def test_describe_gives_both_gains_with_their_errors():
    text = describe(compare([_g("a", 9), _g("b", 4)], [_g("a", 4), _g("b", 4)]))
    assert text.startswith("50.00% wins against 0.00% (+50.00 +/- 50.00 points)")
    assert "(+2.500 +/- 2.500), 2 seeds" in text
