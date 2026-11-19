"""Generated hands and joker lineups for checking the scoring kernel against itself
and against its saved answers (tests/kernel_expected.json).

A case is plain data (raw card and joker dicts as the game sends them, hand levels,
search settings) built from one integer seed, so a failing case is reproduced from
its seed alone. ``search_result``, ``score_results`` and ``kernel_answer`` run the
kernel on a case and return plain values that compare exactly.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import zlib
from dataclasses import dataclass, field

from balatro_bot.constants import HAND_INFO
from balatro_bot.domain.models.card import card_from_dict
from balatro_bot.domain.models.joker import joker_from_dict
from balatro_bot.domain.scoring.estimate import score_hand, score_hand_detailed
from balatro_bot.domain.scoring.kernel import KernelScorer, joker_phases
from balatro_bot.domain.scoring.play_order import sort_play_order
from balatro_bot.domain.scoring.search import best_hand, enumerate_hands
from balatro_bot.joker_effects.live_values import enrich_state

RANKS = "23456789TJQKA"
SUITS = "HDCS"
HAND_NAMES = list(HAND_INFO)

# Jokers that act outside the joker phase, so joker_phases() does not list them.
_OUTSIDE_JOKER_PHASE = [
    "j_greedy_joker", "j_lusty_joker", "j_wrathful_joker", "j_gluttenous_joker", "j_onyx_agate",
    "j_fibonacci", "j_even_steven", "j_odd_todd", "j_scholar", "j_walkie_talkie", "j_smiley",
    "j_scary_face", "j_arrowhead", "j_photograph", "j_triboulet", "j_ancient", "j_idol",
    "j_bloodstone", "j_hiker",
    "j_hack", "j_sock_and_buskin", "j_hanging_chad", "j_dusk", "j_selzer",
    "j_baron", "j_shoot_the_moon", "j_raised_fist", "j_mime",
    "j_midas_mask",
    "j_pareidolia", "j_smeared", "j_oops", "j_four_fingers", "j_shortcut", "j_splash", "j_dna",
]
_NO_SCORING_EFFECT = ["j_egg", "j_credit_card", "j_golden"]
SCORING_KEYS = sorted(set(joker_phases()) | set(_OUTSIDE_JOKER_PHASE))
JOKER_KEYS = SCORING_KEYS + _NO_SCORING_EFFECT

# Every ability field the kernel reads, with values worth trying. A field is left
# out of a joker half the time.
_ABILITY_VALUES = {
    "extra": [0, 1, 2, 3, 5, 8, 20, 31, 50, 0.25, 0.5, 1.5, 2.0],
    "mult": [0, 1, 3, 4, 8, 15, 20, 2.5],
    "t_mult": [0, 8, 10, 12],
    "s_mult": [3, 4],
    "chips": [0, 10, 40, 100, 250, 37.5],
    "chip_mod": [1, 4, 5, 8, 15],
    "Xmult": [1, 1.5, 2, 2.25, 3, 4],
    "x_mult": [1, 1.2, 1.75, 2, 3.5],
    "loyalty_remaining": [0, 1, 3, 5],
    "size": [3, 4],
    "odds": [1, 2, 3, 4],
    "min": [0, 5],
    "max": [10, 23],
    "hand_add": [1, 2],
    "driver_tally": [0, 15, 16, 30],
    "dollars": [5, 10],
    "d_remaining": [0, 1, 3],
    "yorick_discards": [0, 1, 2, 23],
    "invis_rounds": [0, 2],
    "steel_tally": [0, 3],
    "caino_xmult": [1, 3],
}
_BLINDS = ["", "", "", "The Hook", "The Ox", "The Tooth", "The Psychic", "The Wall"]

# Lineups whose jokers only matter together, which random picks rarely produce.
_COMBOS = [
    ("j_flower_pot", "j_smeared"),
    ("j_lucky_cat", "j_hack", "j_sock_and_buskin", "j_hanging_chad", "j_dusk", "j_selzer"),
    ("j_four_fingers", "j_shortcut", "j_tribe", "j_order", "j_runner"),
    ("j_dna", "j_hanging_chad", "j_photograph", "j_pareidolia"),
    ("j_family", "j_trio", "j_duo", "j_trousers", "j_obelisk"),
    ("j_splash", "j_blackboard", "j_seeing_double", "j_smeared"),
    ("j_midas_mask", "j_vampire", "j_blueprint", "j_brainstorm"),
    ("j_baseball", "j_blueprint", "j_baron", "j_mime", "j_raised_fist"),
    ("j_lucky_cat", "j_oops", "j_bloodstone", "j_misprint"),
    ("j_hiker", "j_hack", "j_selzer", "j_wee"),
    ("j_steel_joker", "j_shoot_the_moon", "j_baron", "j_mime"),
    ("j_ride_the_bus", "j_pareidolia", "j_smiley", "j_scary_face", "j_sock_and_buskin"),
]
_ENHANCEMENTS = ["BONUS", "MULT", "WILD", "GLASS", "STEEL", "STONE", "GOLD", "LUCKY"]


@dataclass
class Case:
    seed: int
    hand: list[dict]
    jokers: list[dict]
    levels: dict[str, dict]
    search: dict = field(default_factory=dict)


def _card(rng: random.Random, index: int, plain: bool, ranks: str = RANKS, suits: str = SUITS,
          mostly: str | None = None) -> dict:
    rank, suit = rng.choice(ranks), rng.choice(suits)
    modifier: dict = {}
    value: dict = {"rank": rank, "suit": suit}
    state: dict = {}
    if not plain:
        enhancement = rng.choice([None] * 5 + _ENHANCEMENTS)
        if mostly and rng.random() < 0.7:
            enhancement = mostly
        if enhancement:
            modifier["enhancement"] = enhancement
        if enhancement == "STONE" and rng.random() < 0.5:
            value = {"rank": None, "suit": None}
        if enhancement == "GLASS" and rng.random() < 0.5:
            modifier["enhancement_x_mult"] = 2
        edition = rng.choice([None] * 6 + ["FOIL", "HOLO", "POLYCHROME"])
        if not edition and rng.random() < 0.03:
            modifier[rng.choice(["edition_mult", "edition_x_mult"])] = 3
        if edition:
            modifier["edition"] = edition
            if rng.random() < 0.5:
                name, number = {"FOIL": ("edition_chips", 50), "HOLO": ("edition_mult", 10),
                                "POLYCHROME": ("edition_x_mult", 1.5)}[edition]
                modifier[name] = number
        seal = rng.choice([None] * 6 + ["RED", "GOLD", "BLUE", "PURPLE"])
        if seal:
            modifier["seal"] = seal
        if rng.random() < 0.15:
            value["perma_bonus"] = rng.choice([5, 10, 35])
        if rng.random() < 0.12:
            state["debuff"] = True
    return {
        "id": index, "key": f"{suit}_{rank}", "set": "DEFAULT", "label": f"{rank} of {suit}",
        "value": value, "modifier": modifier or [], "state": state or [], "cost": {},
    }


def _joker(rng: random.Random, key: str, plain: bool = False) -> dict:
    ability = {}
    if not plain:
        for name, values in _ABILITY_VALUES.items():
            if rng.random() < 0.5:
                ability[name] = rng.choice(values)
    joker: dict = {
        "key": key, "label": key, "set": "JOKER",
        "cost": {"sell": rng.choice([0, 1, 2, 3, 5, 10])},
        "value": {"ability": ability, "rarity": rng.choice([1, 2, 2, 3, 4, "Uncommon", None])},
        "modifier": [], "state": [],
    }
    if not plain:
        edition = rng.choice([None] * 5 + ["FOIL", "HOLO", "POLYCHROME", "NEGATIVE"])
        if edition:
            joker["modifier"] = {"edition": edition}
            if rng.random() < 0.3:
                joker["modifier"].update(edition_chips=50, edition_mult=10, edition_x_mult=1.5)
        elif rng.random() < 0.05:
            joker["modifier"] = {"edition_x_mult": 2}
        if rng.random() < 0.1:
            joker["state"] = {"debuff": True}
    return joker


def _levels(rng: random.Random) -> dict[str, dict]:
    names = list(HAND_NAMES)
    rng.shuffle(names)
    levels = {}
    for name in names:
        chips, mult, _ = HAND_INFO[name]
        level = rng.choice([1, 1, 1, 2, 3, 7])
        levels[name] = {
            "chips": chips + 15 * (level - 1), "mult": mult + 2 * (level - 1), "level": level,
            "played": rng.choice([0, 0, 1, 3, 9]), "played_this_round": rng.choice([0, 0, 1]),
        }
    return levels


def _search_settings(rng: random.Random, hand_size: int) -> dict:
    settings: dict = {
        "money": rng.choice([-4, 0, 3, 12, 27, 60]),
        "discards_left": rng.randrange(0, 5),
        "hands_left": rng.randrange(1, 5),
        "joker_limit": rng.choice([5, 5, 6]),
        "deck_count": rng.choice([0, 20, 44]),
        "blind_name": rng.choice(_BLINDS),
    }
    if settings["blind_name"] == "The Ox":
        settings["ox_most_played"] = rng.choice(HAND_NAMES)
    if rng.random() < 0.5:
        settings["ancient_suit"] = rng.choice(SUITS)
    if rng.random() < 0.5:
        settings["idol_rank"], settings["idol_suit"] = rng.choice(RANKS), rng.choice(SUITS)
    if rng.random() < 0.1:
        settings["required_hand"] = rng.choice(HAND_NAMES[:6])
    if rng.random() < 0.1:
        settings["excluded_hands"] = set(rng.sample(HAND_NAMES, 2))
    if rng.random() < 0.1:
        settings["required_card_indices"] = {rng.randrange(hand_size)}
    if rng.random() < 0.1:
        settings["min_select"] = min(5, hand_size)
    if rng.random() < 0.2:
        settings["hand_affinity"] = {name: rng.choice([0.5, 1.0, 2.0]) for name in rng.sample(HAND_NAMES, 3)}
    return settings


def _enrich(rng: random.Random, jokers: list[dict], money: int) -> None:
    """Give the jokers their ``current`` values the way the bot does on every game state."""
    enrich_state({
        "jokers": {"cards": jokers}, "money": money,
        "counters": {"starting_deck_size": 52, "playing_cards": rng.choice([40, 52, 60]),
                     "planets_used_unique": rng.randrange(0, 6), "tarots_used": rng.randrange(0, 9)},
    })


HAND_SIZES = [1, 2, 3, 4, 5, 6, 7, 8, 8, 8, 8, 9, 9, 10]


def random_case(seed: int, hand_sizes: list[int] = HAND_SIZES) -> Case:
    rng = random.Random(seed)
    hand_size = rng.choice(hand_sizes)
    plain_cards = rng.random() < 0.25
    # A third of the hands draw from few ranks or suits, so sets, flushes and
    # five of a kind turn up as often as junk.
    ranks, suits = RANKS, SUITS
    theme = rng.random()
    if theme < 0.12:
        ranks = "".join(rng.sample(RANKS, rng.choice([1, 2, 3])))
    elif theme < 0.24:
        suits = rng.choice(SUITS)
    elif theme < 0.33:
        ranks, suits = "".join(rng.sample(RANKS, 2)), rng.choice(SUITS)
    # Some hands lean on one enhancement, which the jokers that read it need.
    mostly = rng.choice(_ENHANCEMENTS) if rng.random() < 0.2 else None
    hand = [_card(rng, i, plain_cards and not mostly, ranks, suits, mostly) for i in range(hand_size)]
    if rng.random() < 0.3:
        keys = list(rng.choice(_COMBOS))
        rng.shuffle(keys)
        keys += [rng.choice(JOKER_KEYS) for _ in range(rng.choice([0, 1, 2]))]
    else:
        keys = [rng.choice(JOKER_KEYS) for _ in range(rng.choice([0, 1, 2, 3, 4, 5, 5, 6, 8]))]
    jokers = [_joker(rng, key) for key in keys]
    settings = _search_settings(rng, hand_size)
    if jokers and rng.random() < 0.6:
        _enrich(rng, jokers, settings["money"])
    return Case(seed, hand, jokers, _levels(rng), settings)


def pair_case(first: str, second: str, variant: int) -> Case:
    """Two named jokers together; the variant adds a copier, Baseball Card or a debuff."""
    rng = random.Random(f"{first}|{second}|{variant}")
    jokers = [_joker(rng, first, plain=variant == 0), _joker(rng, second, plain=variant == 0)]
    if variant == 1:
        jokers.insert(0, _joker(rng, "j_blueprint"))
    elif variant == 2:
        jokers.append(_joker(rng, "j_brainstorm"))
    elif variant == 3:
        jokers.append(_joker(rng, "j_baseball"))
    elif variant == 4:
        jokers[0]["state"] = {"debuff": True}
        jokers.insert(0, _joker(rng, "j_blueprint"))
    hand = [_card(rng, i, plain=False) for i in range(8)]
    settings = _search_settings(rng, len(hand))
    if variant:
        _enrich(rng, jokers, settings["money"])
    seed = zlib.crc32(f"{first}|{second}|{variant}".encode())
    return Case(seed, hand, jokers, _levels(rng), settings)


# Jokers that change how other jokers score.
_PARTNERS = ["j_oops", "j_pareidolia", "j_smeared", "j_blueprint", "j_brainstorm", "j_four_fingers",
             "j_splash", "j_hanging_chad", "j_mime", "j_baseball", "j_dusk", "j_midas_mask", "j_vampire"]


def targeted_case(key: str, variant: int) -> Case:
    """One named joker with two of the jokers that change how others score, over a hand
    leaning on one enhancement (the variant picks which, and which partners)."""
    rng = random.Random(f"{key}|target|{variant}")
    mostly = (_ENHANCEMENTS + [None])[variant % (len(_ENHANCEMENTS) + 1)]
    partners = [_PARTNERS[(variant + k) % len(_PARTNERS)] for k in (0, 5)]
    keys = [partners[0], key, partners[1]] if variant % 2 else [key, partners[0], partners[1]]
    jokers = [_joker(rng, k, plain=variant % 3 == 0) for k in keys]
    ranks = RANKS if variant % 4 else "".join(rng.sample(RANKS, 3))
    hand = [_card(rng, i, plain=False, ranks=ranks, mostly=mostly) for i in range(8)]
    settings = _search_settings(rng, len(hand))
    settings["hands_left"] = 1 if variant % 2 else settings["hands_left"]
    if variant % 3:
        _enrich(rng, jokers, settings["money"])
    return Case(zlib.crc32(f"{key}|target|{variant}".encode()), hand, jokers, _levels(rng), settings)


def materialise(case: Case, typed: bool) -> tuple[list, list]:
    """Fresh hand and joker objects for one run: typed models or raw dicts."""
    hand, jokers = copy.deepcopy(case.hand), copy.deepcopy(case.jokers)
    if typed:
        return [card_from_dict(c) for c in hand], [joker_from_dict(j) for j in jokers]
    return hand, jokers


def _summary(candidate, positions: dict[int, int]) -> tuple:
    return (candidate.hand_name, tuple(candidate.card_indices), candidate.chips, candidate.mult,
            candidate.total, tuple(positions[id(c)] for c in candidate.scoring_cards))


def search_result(case: Case, typed: bool) -> tuple[list[tuple], tuple | None]:
    """Every allowed play, best first, and the single best play."""
    hand, jokers = materialise(case, typed)
    positions = {id(c): i for i, c in enumerate(hand)}
    every = enumerate_hands(hand, case.levels, jokers=jokers or None, **case.search)
    best = best_hand(hand, case.levels, jokers=jokers or None, **case.search)
    return [_summary(c, positions) for c in every], (_summary(best, positions) if best else None)


_SCORE_SETTINGS = ("money", "discards_left", "hands_left", "joker_limit", "ancient_suit",
                   "deck_count", "blind_name", "ox_most_played", "idol_rank", "idol_suit")


def score_results(case: Case, typed: bool, plays: int = 3) -> list[tuple]:
    """A few plays from the case scored one at a time: by expected value, with seeded
    rolls, and with the per-joker breakdown."""
    hand, jokers = materialise(case, typed)
    settings = {k: v for k, v in case.search.items() if k in _SCORE_SETTINGS}
    every = enumerate_hands(hand, case.levels, jokers=jokers or None, **case.search)
    rng = random.Random(case.seed)
    out = []
    for candidate in rng.sample(every, min(plays, len(every))):
        indices = list(candidate.card_indices)
        order = sort_play_order(indices, hand, jokers) if jokers else indices
        played = [hand[i] for i in order]
        held = [c for i, c in enumerate(hand) if i not in candidate.card_indices]
        args = (candidate.hand_name, candidate.scoring_cards, case.levels)
        common = dict(jokers=jokers or None, played_cards=played, held_cards=held, **settings)
        expected = score_hand(*args, **common)
        rolled = score_hand(*args, rng=random.Random(case.seed), **common)
        detail = score_hand_detailed(*args, **common)
        # The shop scores sample hands whose played list need not hold every scoring card.
        short = score_hand(*args, **{**common, "played_cards": played[:2]})
        out.append((candidate.hand_name, tuple(order), expected, rolled, detail, short))
    return out


# --- Saved answers ------------------------------------------------------------

LARGE_SIZES = [17, 17, 18, 19, 20, 22]


def large_case(seed: int) -> Case:
    """A hand past sixteen cards (Turtle Bean, Juggler, the hand-size vouchers); odd
    seeds force a card that sits past the sixteenth place."""
    case = random_case(seed, LARGE_SIZES)
    if seed % 2:
        case.search["required_card_indices"] = {random.Random(seed).randrange(16, len(case.hand))}
    return case


def saved_cases():
    """(name, case) for every case in tests/kernel_expected.json, in file order."""
    for seed in range(300):
        yield f"random {seed}", random_case(seed)
    for seed in range(16):
        yield f"large {seed}", large_case(seed)
    keys = SCORING_KEYS[::9]
    for first in keys:
        for second in keys:
            for variant in range(5):
                yield f"pair {first} {second} {variant}", pair_case(first, second, variant)
    for key in SCORING_KEYS:
        for variant in range(13):
            yield f"target {key} {variant}", targeted_case(key, variant)


def kernel_scorer(case: Case, jokers: list) -> KernelScorer:
    settings = {k: v for k, v in case.search.items() if k in _SCORE_SETTINGS}
    return KernelScorer(case.levels, jokers, **settings)


_BREAKDOWN_KEYS = ("pre_joker_chips", "pre_joker_mult", "joker_contributions",
                   "post_joker_chips", "post_joker_mult", "total")


def _floats(value):
    """The value with every number a float, so 115 and 115.0 hash alike."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_floats(v) for v in value]
    return value


def kernel_answer(name: str, case: Case, typed: bool, plays: int = 3) -> dict:
    """What the saved file holds for one case: how many plays the search finds and a
    digest of them, the best play, and a few plays scored by expected value, with
    seeded rolls, with the per-joker breakdown and with a short played list."""
    hand, jokers = materialise(case, typed)
    every, best = search_result(case, typed)
    scorer = kernel_scorer(case, jokers)
    rng = random.Random(case.seed)
    scores = []
    full = enumerate_hands(hand, case.levels, jokers=jokers or None, **case.search)
    for candidate in rng.sample(full, min(plays, len(full))):
        indices = list(candidate.card_indices)
        order = sort_play_order(indices, hand, jokers) if jokers else indices
        played = [hand[i] for i in order]
        held = [c for i, c in enumerate(hand) if i not in candidate.card_indices]
        name_, scoring = candidate.hand_name, candidate.scoring_cards
        breakdown = scorer.breakdown(name_, scoring, played, held)
        scores.append((
            scorer.score(name_, scoring, played, held),
            scorer.score(name_, scoring, played, held, rng=random.Random(case.seed)),
            {k: breakdown[k] for k in _BREAKDOWN_KEYS},
            scorer.score(name_, scoring, played[:2], held),
        ))
    return json.loads(json.dumps({
        "name": name, "count": len(every),
        "digest": hashlib.sha256(json.dumps(_floats(every)).encode()).hexdigest()[:16],
        "best": best, "scores": scores,
    }))
