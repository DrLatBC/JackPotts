"""Big starts: training games that start from just before the best hands played so far.

A main line keeps a copy of its game at each of its last ``BACK`` build decisions (a shop
move, a pack pick, a blind select: anything outside a round's play). When it plays a hand
scoring at least ``MIN_RATIO`` times its blind's target, the oldest of those copies goes
into the pool, which keeps the ``size`` best by that ratio. Once the pool holds ``FULL``
entries, a share of new main lines start from a pool copy instead of a fresh seed. Each
use multiplies an entry's weight by ``DECAY``, so newer entries are drawn before ones
already replayed many times.

The settings follow wardn's "big-starts" run (``big_starts_back: 9``,
``big_starts_full: 128``, ``big_starts_decay: 0.6``); his code is private, so how each
works here is our own reading. His minimum is 20 times the target, but night5 u7 played no
such hand in 300 games (5x or more: 0.4% of hands), so ours is 5.
"""

from __future__ import annotations

import gzip
import json
from collections import deque
from pathlib import Path

import numpy as np
from jackdaw.core.game import Game

BACK = 9
MIN_RATIO = 5.0
FULL = 128
DECAY = 0.6
SIZE = 200
SHARE = 0.25
BUILD_PHASES = frozenset({"shop", "pack_opening", "blind_select"})


def history() -> deque:
    """A main line's copies of its game at its last ``BACK`` build decisions."""
    return deque(maxlen=BACK)


def note_decision(game, kept: deque) -> None:
    """Keep a copy of ``game`` if it is at a build decision."""
    if game.phase.value in BUILD_PHASES:
        kept.append(game.clone())


class StartPool:
    """The best starting points found so far, by the score over target of the hand that
    followed them."""

    def __init__(self, size: int = SIZE, *, min_ratio: float = MIN_RATIO, full: int = FULL,
                 decay: float = DECAY) -> None:
        self.size, self.min_ratio, self.full, self.decay = size, min_ratio, full, decay
        self.entries: list[dict] = []
        self.used = 0

    def offer(self, ratio: float, kept: deque) -> bool:
        """Add the oldest kept copy for a hand that scored ``ratio`` times its target, if
        the ratio qualifies and beats the pool's lowest; False otherwise."""
        if not ratio >= self.min_ratio or not kept:
            return False
        if len(self.entries) >= self.size and ratio <= self.entries[-1]["ratio"]:
            return False
        game = kept[0]
        key = game.state_hash()
        same = next((e for e in self.entries if e["hash"] == key), None)
        if same is not None:
            same["ratio"] = max(same["ratio"], float(ratio))
            self.entries.sort(key=lambda e: -e["ratio"])
            return False
        self.entries.append({"ratio": float(ratio), "weight": 1.0, "hash": key, "game": game.clone()})
        self.entries.sort(key=lambda e: -e["ratio"])
        del self.entries[self.size:]
        return True

    @property
    def ready(self) -> bool:
        return len(self.entries) >= self.full

    def draw(self, rng: np.random.Generator):
        """A copy of an entry's game, drawn by weight; the entry's weight then decays."""
        w = np.array([e["weight"] for e in self.entries])
        entry = self.entries[int(rng.choice(len(w), p=w / w.sum()))]
        entry["weight"] *= self.decay
        self.used += 1
        return entry["game"].clone()

    def stats(self) -> dict:
        ratios = [e["ratio"] for e in self.entries]
        return {"pool": len(ratios), "pool low": min(ratios, default=None),
                "pool high": max(ratios, default=None), "pool used": self.used}

    def save(self, path: Path) -> None:
        rows = [{"ratio": e["ratio"], "weight": e["weight"], "game": e["game"].to_dict()}
                for e in self.entries]
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"used": self.used, "entries": rows}, f)
        tmp.replace(path)

    def load(self, path: Path) -> None:
        if not path.exists():
            return
        with gzip.open(path, "rt", encoding="utf-8") as f:
            saved = json.load(f)
        self.used = saved["used"]
        self.entries = []
        for row in saved["entries"]:
            game = Game.from_dict(row["game"])
            self.entries.append({"ratio": row["ratio"], "weight": row["weight"],
                                 "hash": game.state_hash(), "game": game})
