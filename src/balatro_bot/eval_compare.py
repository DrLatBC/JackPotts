"""Two models' results on the same evaluation seeds, compared seed by seed.

Both the control panel and ``rl.ppo`` read this; it needs nothing beyond the standard
library. A game here is a row of ``rl.evaluate.play``: at least ``seed``, ``won`` and
``ante`` (9 for a win).
"""

from __future__ import annotations

import math


def _mean_se(values: list[float]) -> tuple[float, float]:
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, math.sqrt(var / n)


def compare(games: list[dict], base: list[dict]) -> dict | None:
    """``games`` against ``base`` on the seeds both played; None when they share none.

    Each seed gives one difference in final ante and one in wins (1, 0 or -1), so the
    luck of the seed cancels; the standard errors are of those per-seed differences.
    """
    by_seed = {g["seed"]: g for g in base}
    pairs = [(g, by_seed[g["seed"]]) for g in games if g["seed"] in by_seed]
    if not pairs:
        return None
    ante, ante_se = _mean_se([g["ante"] - b["ante"] for g, b in pairs])
    wins, wins_se = _mean_se([float(bool(g["won"])) - float(bool(b["won"])) for g, b in pairs])
    n = len(pairs)
    return {
        **_endless(pairs),
        "seeds": n,
        "win rate": sum(bool(g["won"]) for g, _ in pairs) / n,
        "base win rate": sum(bool(b["won"]) for _, b in pairs) / n,
        "mean final ante": sum(g["ante"] for g, _ in pairs) / n,
        "base mean final ante": sum(b["ante"] for _, b in pairs) / n,
        "win gain": wins, "win gain +/-": wins_se,
        "ante gain": ante, "ante gain +/-": ante_se,
        "won only here": sum(bool(g["won"]) and not b["won"] for g, b in pairs),
        "won only by base": sum(bool(b["won"]) and not g["won"] for g, b in pairs),
    }


def _endless(pairs: list[tuple[dict, dict]]) -> dict:
    """For games played on into endless mode: the mean ante each side's wins ended on,
    and the gain on the seeds both won. Empty when either side has no endless results."""
    mine = [g["endless ante"] for g, _ in pairs if g["won"] and "endless ante" in g]
    theirs = [b["endless ante"] for _, b in pairs if b["won"] and "endless ante" in b]
    both = [g["endless ante"] - b["endless ante"] for g, b in pairs
            if "endless ante" in g and "endless ante" in b and g["won"] and b["won"]]
    if not (mine and theirs and both):
        return {}
    gain, se = _mean_se(both)
    return {"endless ante": sum(mine) / len(mine), "base endless ante": sum(theirs) / len(theirs),
            "endless gain": gain, "endless gain +/-": se, "both won": len(both)}


def describe(c: dict) -> str:
    """One line: wins and mean final ante against the base, with the gains."""
    text = (f"{c['win rate']:.2%} wins against {c['base win rate']:.2%} "
            f"({c['win gain'] * 100:+.2f} +/- {c['win gain +/-'] * 100:.2f} points), "
            f"mean final ante {c['mean final ante']:.2f} against {c['base mean final ante']:.2f} "
            f"({c['ante gain']:+.3f} +/- {c['ante gain +/-']:.3f}), {c['seeds']:,} seeds")
    if "endless ante" in c:
        text += (f"; wins went on to ante {c['endless ante']:.2f} against {c['base endless ante']:.2f} "
                 f"({c['endless gain']:+.2f} +/- {c['endless gain +/-']:.2f} on the "
                 f"{c['both won']:,} seeds both won)")
    return text
