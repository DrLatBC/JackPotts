"""Exploration: sometimes take a runner-up shop plan or pack pick.

Decision records from greedy play only show what the current valuation
already likes. On exploring instances, a fraction of shop and pack decisions
pick uniformly among the top-ranked options instead, and each record carries
the probability of the option taken so a learner can reweight for it.

The first explored pick that points the run somewhere (a joker's hands,
suits or ranks, a planet's hand, a suit tarot's suit) sets the run's lean.
compute_strategy adds the lean to every strategy built for the rest of the
run, so the direction gets played out instead of being dropped next shop,
and later explorations skip options that point elsewhere.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

TOP_K = 5

# {"hands": {name: weight}, "suits": {...}, "ranks": {...}}; empty channels omitted.
Lean = dict[str, dict[str, float]]


@dataclass(frozen=True)
class Choice:
    """Which ranked option to take, and how it was chosen."""
    idx: int            # position in the value-ranked list; 0 is the greedy pick
    policy: str         # "greedy", or "explore" when the exploration roll fired
    propensity: float   # probability this option was taken, given the ranked list


class Explorer:
    def __init__(self, rate: float = 0.0, top_k: int = TOP_K) -> None:
        self.rate = rate
        self.top_k = top_k
        self._rng = random.Random()
        self.lean: Lean | None = None

    def reseed(self, seed: object) -> None:
        """Start a run: seed from the game seed so a replay makes the same choices."""
        self._rng.seed(f"explore:{seed}")
        self.lean = None

    def set_lean(self, lean: Lean | None) -> bool:
        """Adopt *lean* if the run has none yet. Returns True when it was adopted."""
        if self.lean is not None or not lean:
            return False
        self.lean = lean
        return True

    def conflicts(self, lean: Lean | None) -> bool:
        """True when *lean* points the run somewhere its lean doesn't."""
        if self.lean is None or not lean:
            return False
        return not any(
            set(lean.get(ch, {})) & set(self.lean.get(ch, {}))
            for ch in ("hands", "suits", "ranks")
        )

    def choose(self, n_ranked: int) -> Choice:
        """Pick an index into a value-ranked list of *n_ranked* options."""
        k = min(self.top_k, n_ranked)
        if self.rate <= 0 or k <= 1:
            return Choice(0, "greedy", 1.0)
        if self._rng.random() >= self.rate:
            return Choice(0, "greedy", (1 - self.rate) + self.rate / k)
        idx = self._rng.randrange(k)
        p = self.rate / k + ((1 - self.rate) if idx == 0 else 0.0)
        return Choice(idx, "explore", p)

    def tag(self, record: dict, choice: Choice) -> None:
        """Stamp a decision record with how it was chosen."""
        record["policy"] = choice.policy
        if self.rate > 0:
            record["propensity"] = round(choice.propensity, 6)
        if self.lean is not None:
            record["lean"] = self.lean


_explorer = Explorer()


def explorer() -> Explorer:
    return _explorer


def configure(rate: float, top_k: int = TOP_K) -> None:
    _explorer.rate = rate
    _explorer.top_k = top_k
