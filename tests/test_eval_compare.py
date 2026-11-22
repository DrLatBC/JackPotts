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


def _e(seed, ante, endless=None):
    g = _g(seed, ante)
    if endless is not None:
        g["endless ante"] = endless
    return g


def test_endless_gain_counts_only_seeds_both_won():
    new = [_e("a", 9, 12), _e("b", 9, 10), _e("c", 9, 11), _e("d", 4, 4)]
    old = [_e("a", 9, 10), _e("b", 9, 11), _e("c", 5, 5), _e("d", 9, 13)]
    c = compare(new, old)
    assert c["both won"] == 2 and c["endless gain"] == pytest.approx((2 - 1) / 2)
    assert c["endless ante"] == pytest.approx(11) and c["base endless ante"] == pytest.approx(34 / 3)
    assert c["endless max"] == 12 and c["base endless max"] == 13
    assert "wins went on to ante 11.00 against 11.33 (+0.50" in describe(c)
    assert describe(c).endswith("furthest ante 12 against 13")


def test_no_endless_numbers_without_endless_games_on_both_sides():
    c = compare([_e("a", 9, 12)], [_e("a", 9)])
    assert "endless ante" not in c and "wins went on" not in describe(c)


def test_describe_gives_both_gains_with_their_errors():
    text = describe(compare([_g("a", 9), _g("b", 4)], [_g("a", 4), _g("b", 4)]))
    assert text.startswith("50.00% wins against 0.00% (+50.00 +/- 50.00 points)")
    assert "(+2.500 +/- 2.500), 2 seeds" in text
