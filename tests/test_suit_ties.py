"""Ties between suits are broken the same way in every process (Wild cards count as every suit)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from balatro_bot.domain.models.deck_profile import DeckProfile
from balatro_bot.domain.scoring.draws import flush_draw_quality
from tests.conftest import card

ROOT = Path(__file__).resolve().parent.parent

# Four hearts and four spades once the Wild card counts as both. The Wild card comes
# first, so a suit order taken from a set would decide which suit is seen first.
HAND = [card("7", "C", "WILD"), card("A", "H"), card("9", "H"), card("4", "H"),
        card("K", "S"), card("8", "S"), card("3", "S"), card("2", "D")]
DECK = [card(r, s) for s in ("H", "S") for r in ("5", "6", "T", "J", "Q")]


def test_flush_chase_tie_goes_to_the_first_suit():
    keep, _, suit = flush_draw_quality(HAND, DECK)
    assert suit == "H"
    assert sorted(keep) == [0, 1, 2, 3]


def test_dominant_suit_tie_goes_to_the_first_suit():
    profile = DeckProfile.from_cards([card("7", "S", "WILD"), card("2", "S"), card("3", "H")])
    assert profile.dominant_suit == "H"
    profile = DeckProfile.from_cards([card("7", "C", "WILD")])
    assert list(profile.suit_counts) == ["H", "D", "C", "S"]


_CHILD = """
from balatro_bot.domain.scoring.draws import flush_draw_quality
from tests.test_suit_ties import DECK, HAND
print(flush_draw_quality(HAND, DECK)[2])
"""


def test_same_choice_under_any_string_hash_seed():
    picks = set()
    for hash_seed in map(str, range(12)):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        out = subprocess.run([sys.executable, "-c", _CHILD], cwd=ROOT, env=env,
                             capture_output=True, text=True, check=True)
        picks.add(out.stdout.strip())
    assert picks == {"H"}
