"""
Joker effect registry for Balatro scoring — all 150 jokers.

Each joker effect is a function that mutates a ScoreContext in place.
Unrecognized joker keys are silently ignored (no effect applied).

Scaling jokers parse their actual current values from the joker's
value.effect field (the UI description text) when available, falling
back to hardcoded estimates if parsing fails.

Economy and utility jokers are registered as no-ops (recognized by the
shop logic as "known" jokers worth buying, even with no direct scoring).

Balatro scoring pipeline:
    1. Start with hand base chips/mult (leveled)
    2. For each scoring card (with retriggers): add chips, add mult,
       then multiply running mult by card xmult (Glass, Polychrome)
    3. Apply held-card effects (Steel ×1.5 mult)
    4. Apply each joker effect in order — xmult multiplies running mult
    5. Final score = chips × mult
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from typing import Any

from hand_evaluator import card_rank, card_suit, card_suits, is_debuffed, rank_value, _modifier


# ---------------------------------------------------------------------------
# Effect text parser — extract actual values from joker UI description
# ---------------------------------------------------------------------------

_CHIPS_PATTERN = re.compile(r'\+(\d+(?:\.\d+)?)\s+Chips')
_MULT_PATTERN = re.compile(r'\+(\d+(?:\.\d+)?)\s+Mult')
_XMULT_PATTERN = re.compile(r'X(\d+(?:\.\d+)?)\s+Mult')
# Scaling xmult jokers show "gains X0.5 Mult... (Currently X1.5 Mult)"
# The "Currently" value is the accumulated total we actually want.
_CURRENTLY_XMULT_PATTERN = re.compile(r'Currently\s+X(\d+(?:\.\d+)?)')
# Scaling additive mult jokers show "gains +3 Mult... (Currently +21 Mult)"
# Some show shortened "(Currently +21)" without the "Mult" suffix.
_CURRENTLY_MULT_PATTERN = re.compile(r'Currently\s+\+(\d+(?:\.\d+)?)\s+Mult')
# Fallback: "Currently +N" without suffix — only used when text mentions "Mult" elsewhere
_CURRENTLY_BARE_PATTERN = re.compile(r'Currently\s+\+(\d+(?:\.\d+)?)\b')
# Scaling chip jokers show "gains +5 Chips... (Currently +37 Chips)"
_CURRENTLY_CHIPS_PATTERN = re.compile(r'Currently\s+\+(\d+(?:\.\d+)?)\s+Chips')


def parse_effect_value(effect_text: str) -> dict[str, float | None]:
    """Extract numeric scoring values from a joker's effect description text.

    The effect text comes from joker["value"]["effect"] and contains the
    joker's current state as displayed in the game UI. For scaling jokers,
    the text contains both the increment ("gains X0.5 Mult") and the current
    accumulated value ("Currently X2.0 Mult"). We prefer the "Currently" value.

    Returns dict with keys 'chips', 'mult', 'xmult' (each float or None).
    """
    if not effect_text:
        return {"chips": None, "mult": None, "xmult": None}

    result: dict[str, float | None] = {"chips": None, "mult": None, "xmult": None}

    # For chips: prefer "Currently +N Chips" (the accumulated total) over the
    # first "+N Chips" match (which may be the per-trigger increment).
    # Affects: Blue Joker, Hiker, Castle, Wee Joker, and other chip-scaling jokers.
    currently_chips_match = _CURRENTLY_CHIPS_PATTERN.search(effect_text)
    if currently_chips_match:
        result["chips"] = float(currently_chips_match.group(1))
    else:
        chips_match = _CHIPS_PATTERN.search(effect_text)
        if chips_match:
            result["chips"] = float(chips_match.group(1))

    # For xmult: prefer "Currently X..." (the accumulated total) over the
    # first "X... Mult" match (which may be the per-trigger increment).
    currently_match = _CURRENTLY_XMULT_PATTERN.search(effect_text)
    if currently_match:
        result["xmult"] = float(currently_match.group(1))
    else:
        # No "Currently" — use first X Mult match. For simple jokers
        # like Cavendish this is "X3 Mult". For decay jokers like Ramen
        # this is "X1.85 Mult" (current value) before the "-X0.01" decrement.
        xmult_match = _XMULT_PATTERN.search(effect_text)
        if xmult_match:
            result["xmult"] = float(xmult_match.group(1))

    # For additive mult: prefer "Currently +N Mult" (accumulated total) over
    # the first "+N Mult" match (which may be the per-trigger increment).
    currently_mult_match = _CURRENTLY_MULT_PATTERN.search(effect_text)
    if currently_mult_match:
        result["mult"] = float(currently_mult_match.group(1))
    else:
        # Some jokers show "Currently +N" without "Mult" suffix (e.g. Fortune Teller).
        # Only use the bare pattern if the text mentions "Mult" elsewhere.
        if "Mult" in effect_text:
            bare_match = _CURRENTLY_BARE_PATTERN.search(effect_text)
            if bare_match:
                result["mult"] = float(bare_match.group(1))
        if result["mult"] is None:
            mult_match = _MULT_PATTERN.search(effect_text)
            if mult_match:
                result["mult"] = float(mult_match.group(1))

    return result


def _get_parsed_value(joker: dict, key: str, fallback: float) -> float:
    """Get a parsed value from joker effect text, falling back to estimate.

    key: one of 'chips', 'mult', 'xmult'
    fallback: the hardcoded estimate to use if parsing fails
    """
    effect_text = joker.get("value", {}).get("effect", "")
    if not effect_text:
        return fallback
    parsed = parse_effect_value(effect_text)
    value = parsed.get(key)
    return value if value is not None else fallback


def _ability(joker: dict) -> dict:
    """Return the joker's ability dict from the API (empty dict if absent)."""
    return joker.get("value", {}).get("ability", {})


def _ab_chips(joker: dict, fallback: float = 0) -> float:
    """Get chip value from ability data, then text parsing, then fallback."""
    ab = _ability(joker)
    v = ab.get("chips")
    if v is not None:
        return float(v)
    return _get_parsed_value(joker, "chips", fallback)


def _ab_mult(joker: dict, fallback: float = 0) -> float:
    """Get mult value from ability data, then text parsing, then fallback."""
    ab = _ability(joker)
    v = ab.get("mult") or ab.get("t_mult")
    if v is not None:
        return float(v)
    return _get_parsed_value(joker, "mult", fallback)


def _ab_xmult(joker: dict, fallback: float = 1.0) -> float:
    """Get xmult value from ability data, then text parsing, then fallback."""
    ab = _ability(joker)
    v = ab.get("Xmult") or ab.get("x_mult")
    if v is not None:
        return float(v)
    return _get_parsed_value(joker, "xmult", fallback)


# ---------------------------------------------------------------------------
# Score context — mutable state passed through the joker pipeline
# ---------------------------------------------------------------------------

@dataclass
class ScoreContext:
    chips: int
    mult: float
    hand_name: str
    scoring_cards: list[dict]
    played_cards: list[dict]
    held_cards: list[dict]
    hand_levels: dict
    jokers: list[dict]
    money: int
    discards_left: int
    hands_left: int
    joker_limit: int = 5
    pareidolia: bool = False  # j_pareidolia: all cards count as face cards
    ancient_suit: str | None = None  # Ancient Joker's current rotating suit (H/D/C/S)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FACE_RANKS = {"J", "Q", "K"}
FIBONACCI_RANKS = {"A", "2", "3", "5", "8"}
EVEN_RANKS = {"T", "8", "6", "4", "2"}
ODD_RANKS = {"A", "9", "7", "5", "3"}


def _count_suit_in_scoring(ctx: ScoreContext, suit: str) -> int:
    """Count scored cards of the given suit, weighted by retrigger count.

    Retrigger jokers (Seltzer, Dusk, etc.) fire per-card effects multiple times,
    so a retriggered Diamond scores Greedy Joker twice, for example.
    """
    return sum(
        retrigger_count(c, ctx)
        for c in ctx.scoring_cards
        if not is_debuffed(c) and suit in card_suits(c)
    )


def _count_face_in_scoring(ctx: ScoreContext) -> int:
    """Count scored face cards, weighted by retrigger count.

    With j_pareidolia, every card counts as a face card.
    """
    return sum(
        retrigger_count(c, ctx)
        for c in ctx.scoring_cards
        if not is_debuffed(c) and (ctx.pareidolia or card_rank(c) in FACE_RANKS)
    )


def _hand_contains(ctx: ScoreContext, *hand_types: str) -> bool:
    """Check if the played hand name implies containing a sub-hand type."""
    name = ctx.hand_name
    for ht in hand_types:
        if name == ht:
            return True
    if "Pair" in hand_types:
        if name in ("Pair", "Two Pair", "Three of a Kind", "Full House",
                     "Four of a Kind", "Five of a Kind", "Flush House", "Flush Five"):
            return True
    if "Three of a Kind" in hand_types:
        if name in ("Three of a Kind", "Full House", "Four of a Kind",
                     "Five of a Kind", "Flush House", "Flush Five"):
            return True
    if "Four of a Kind" in hand_types:
        if name in ("Four of a Kind", "Five of a Kind", "Flush Five"):
            return True
    if "Two Pair" in hand_types:
        if name in ("Two Pair", "Full House", "Flush House"):
            return True
    if "Straight" in hand_types:
        if name in ("Straight", "Straight Flush"):
            return True
    if "Flush" in hand_types:
        if name in ("Flush", "Straight Flush", "Flush House", "Flush Five"):
            return True
    return False


def _noop(ctx: ScoreContext, j: dict) -> None:
    """No scoring effect — economy, utility, or unmodelable joker."""
    pass


# ---------------------------------------------------------------------------
# Retrigger logic
# ---------------------------------------------------------------------------

def retrigger_count(card: dict, ctx: ScoreContext) -> int:
    """How many times a scoring card fires (1 = normal, 2+ = retriggered)."""
    if is_debuffed(card):
        return 1
    count = 1
    rank = card_rank(card)
    joker_keys = {j.get("key") for j in ctx.jokers}

    # Hack: retrigger 2, 3, 4, 5
    if "j_hack" in joker_keys and rank in ("2", "3", "4", "5"):
        count += 1

    # Sock and Buskin: retrigger face cards (all cards with j_pareidolia)
    if "j_sock_and_buskin" in joker_keys and (rank in FACE_RANKS or ctx.pareidolia):
        count += 1

    # Hanging Chad: retrigger first scored card 2 extra times
    if "j_hanging_chad" in joker_keys:
        if ctx.scoring_cards and card is ctx.scoring_cards[0]:
            count += 2

    # Dusk: retrigger all played cards on final hand
    if "j_dusk" in joker_keys and ctx.hands_left == 1:
        count += 1

    # Seltzer: retrigger all played cards (assume active if owned)
    if "j_selzer" in joker_keys:
        count += 1

    # Red seal: retrigger the card once more
    if _modifier(card).get("seal") == "RED":
        count += 1

    return count


# ---------------------------------------------------------------------------
# Effect functions: (ctx, joker) -> None
#
# Organized by joker number for completeness.
# Scaling jokers use estimated mid-run values where we can't track state.
# ---------------------------------------------------------------------------

# #1 j_joker — +4 Mult
def _joker(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("mult", 4)

# #2 j_greedy_joker — +3 Mult per scored Diamond
def _greedy(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("s_mult", 3) * _count_suit_in_scoring(ctx, "D")

# #3 j_lusty_joker — +3 Mult per scored Heart
def _lusty(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("s_mult", 3) * _count_suit_in_scoring(ctx, "H")

# #4 j_wrathful_joker — +3 Mult per scored Spade
def _wrathful(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("s_mult", 3) * _count_suit_in_scoring(ctx, "S")

# #5 j_gluttenous_joker — +3 Mult per scored Club
def _gluttenous(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("s_mult", 3) * _count_suit_in_scoring(ctx, "C")

# #6 j_jolly — +8 Mult if Pair
def _jolly(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Pair"):
        ctx.mult += _ability(j).get("t_mult", 8)

# #7 j_zany — +12 Mult if Three of a Kind
def _zany(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Three of a Kind"):
        ctx.mult += _ability(j).get("t_mult", 12)

# #8 j_mad — +10 Mult if Two Pair
def _mad(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Two Pair"):
        ctx.mult += _ability(j).get("t_mult", 10)

# #9 j_crazy — +12 Mult if Straight
def _crazy(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Straight"):
        ctx.mult += _ability(j).get("t_mult", 12)

# #10 j_droll — +10 Mult if Flush
def _droll(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Flush"):
        ctx.mult += _ability(j).get("t_mult", 10)

# #11 j_sly — +50 Chips if Pair
def _sly(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Pair"):
        ctx.chips += _ability(j).get("t_chips", 50)

# #12 j_wily — +100 Chips if Three of a Kind
def _wily(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Three of a Kind"):
        ctx.chips += _ability(j).get("t_chips", 100)

# #13 j_clever — +80 Chips if Two Pair
def _clever(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Two Pair"):
        ctx.chips += _ability(j).get("t_chips", 80)

# #14 j_devious — +100 Chips if Straight
def _devious(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Straight"):
        ctx.chips += _ability(j).get("t_chips", 100)

# #15 j_crafty — +80 Chips if Flush
def _crafty(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Flush"):
        ctx.chips += _ability(j).get("t_chips", 80)

# #16 j_half — +20 Mult if 3 or fewer cards played
def _half(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    if len(ctx.played_cards) <= ab.get("size", 3):
        ctx.mult += ab.get("mult", 20)

# #17 j_stencil — X1 Mult per empty joker slot (including itself)
def _stencil(ctx: ScoreContext, j: dict) -> None:
    # +1 because Stencil "counts itself as empty"
    empty = (ctx.joker_limit - len(ctx.jokers)) + 1
    ctx.mult *= max(1, empty)

# #18 j_four_fingers — 4-card Flushes/Straights (utility, needs evaluator change)
# Registered as noop; real effect requires hand_evaluator changes

# #19 j_mime — retrigger held-in-hand abilities (handled elsewhere)

# #20 j_credit_card — economy (-$20 debt)

# #21 j_ceremonial — scaling +Mult (destroys right joker)
def _ceremonial(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ab_mult(j, fallback=10)

# #22 j_banner — +30 Chips per discard remaining
def _banner(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ability(j).get("extra", 30) * ctx.discards_left

# #23 j_mystic_summit — +15 Mult if 0 discards left
def _mystic_summit(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    if ctx.discards_left <= ab.get("d_remaining", 0):
        ctx.mult += ab.get("mult", 15)

# #24 j_marble — adds Stone card on blind select (utility)

# #25 j_loyalty_card — X4 Mult every 6th hand
def _loyalty_card(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    remaining = ab.get("loyalty_remaining")
    if remaining is not None:
        # API tells us exactly: 0 remaining = triggers this hand
        if remaining == 0:
            ctx.mult *= ab.get("Xmult", 4)
    else:
        # No API data — use expected value
        ctx.mult *= 1.5

# #26 j_8_ball — utility (tarot generation)

# #27 j_misprint — +0 to +23 Mult (random, must estimate)
def _misprint(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    # Truly random — expected value is (min+max)/2
    lo = ab.get("min", 0)
    hi = ab.get("max", 23)
    ctx.mult += (lo + hi) / 2

# #28 j_dusk — retrigger on final hand (handled in retrigger_count)

# #29 j_raised_fist — 2x rank of lowest held card as Mult
def _raised_fist(ctx: ScoreContext, j: dict) -> None:
    held_ranks = [rank_value(card_rank(c)) for c in ctx.held_cards if card_rank(c)]
    if held_ranks:
        ctx.mult += 2 * min(held_ranks)

# #30 j_chaos — economy (1 free reroll)

# #31 j_fibonacci — +8 Mult per scored A/2/3/5/8
def _fibonacci(ctx: ScoreContext, j: dict) -> None:
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) in FIBONACCI_RANKS)
    ctx.mult += _ability(j).get("extra", 8) * count

# #32 j_steel_joker — X0.2 per Steel Card in deck
def _steel_joker(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=1.2)

# #33 j_scary_face — +30 Chips per scored face card
def _scary_face(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ability(j).get("extra", 30) * _count_face_in_scoring(ctx)

# #34 j_abstract — +3 Mult per joker owned
def _abstract(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("extra", 3) * len(ctx.jokers)

# #35 j_delayed_grat — economy ($2 per unused discard)

# #36 j_hack — retrigger 2/3/4/5 (handled in retrigger_count)

# #37 j_pareidolia — all cards are face cards (utility, needs evaluator)

# #38 j_gros_michel — +15 Mult (1/6 destroyed)
def _gros_michel(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("mult", 15)

# #39 j_even_steven — +4 Mult per scored even card
def _even_steven(ctx: ScoreContext, j: dict) -> None:
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) in EVEN_RANKS)
    ctx.mult += _ability(j).get("extra", 4) * count

# #40 j_odd_todd — +31 Chips per scored odd card
def _odd_todd(ctx: ScoreContext, j: dict) -> None:
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) in ODD_RANKS)
    ctx.chips += _ability(j).get("extra", 31) * count

# #41 j_scholar — +20 Chips +4 Mult per scored Ace
def _scholar(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) == "A")
    ctx.chips += ab.get("chips", 20) * count
    ctx.mult += ab.get("mult", 4) * count

# #42 j_business — economy (1/2 chance $2 per face card)

# #43 j_supernova — +Mult = times hand played this run
def _supernova(ctx: ScoreContext, j: dict) -> None:
    # Supernova's mult = number of times current hand type was played this run.
    # Available from hand_levels[hand_name]["played"] + 1 (current play counts).
    played = ctx.hand_levels.get(ctx.hand_name, {}).get("played", 0) + 1
    ctx.mult += played

# #44 j_ride_the_bus — +1 Mult per hand without face card
def _ride_the_bus(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ab_mult(j, fallback=5)

# #45 j_space — utility (1/4 hand level upgrade)

# #46 j_egg — economy (+$3 sell value/round)

# #47 j_burglar — utility (+3 hands, lose discards)

# #48 j_blackboard — X3 if all held cards are Spades/Clubs
def _blackboard(ctx: ScoreContext, j: dict) -> None:
    if ctx.held_cards and all(
        card_suits(c) & {"S", "C"} for c in ctx.held_cards if card_suits(c)
    ):
        ctx.mult *= _ability(j).get("extra", 3.0)

# #49 j_runner — scaling +15 Chips per Straight
def _runner(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ab_chips(j, fallback=30)

# #50 j_ice_cream — +100 Chips, -5 per hand
def _ice_cream(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ab_chips(j, fallback=60)

# #51 j_dna — utility (copy single-card first hand)

# #52 j_splash — utility (all played cards score). Needs evaluator change.

# #53 j_blue_joker — +2 Chips per remaining deck card
def _blue_joker(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ab_chips(j, fallback=70)

# #54 j_sixth_sense — utility (single 6 → Spectral)

# #55 j_constellation — scaling X0.1 per Planet used
def _constellation(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=1.3)

# #56 j_hiker — +5 Chips permanent on played cards
def _hiker(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ab_chips(j, fallback=10)

# #57 j_faceless — economy ($5 if 3+ face cards discarded)

# #58 j_green_joker — +1 Mult per hand, -1 per discard
def _green_joker(ctx: ScoreContext, j: dict) -> None:
    # Green Joker gains +hand_add before scoring, but our snapshot is pre-play.
    # Compensate by adding the per-hand increment to the parsed/ability value.
    ab = _ability(j)
    ctx.mult += _ab_mult(j, fallback=5) + ab.get("hand_add", 1)

# #59 j_superposition — utility (Tarot if Ace + Straight)

# #60 j_todo_list — economy ($4 if matching target hand)

# #61 j_cavendish — X3 Mult (1/1000 destroyed)
def _cavendish(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=3.0)

# #62 j_card_sharp — X3 if hand type repeated this round
def _card_sharp(ctx: ScoreContext, j: dict) -> None:
    # Card Sharp triggers if this hand type was already played this round.
    # We can check hand_levels[hand_name].played_this_round from the state.
    played_count = ctx.hand_levels.get(ctx.hand_name, {}).get("played_this_round", 0)
    if played_count > 0:
        ctx.mult *= _ab_xmult(j, fallback=3.0)

# #63 j_red_card — scaling +3 Mult per pack skipped
def _red_card(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ab_mult(j, fallback=6)

# #64 j_madness — scaling X0.5 per blind select, destroys joker
def _madness(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=2.0)

# #65 j_square — scaling +4 Chips if exactly 4 cards
def _square(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ab_chips(j, fallback=20)

# #66 j_seance — utility (Spectral if Straight Flush)

# #67 j_riff_raff — utility (2 Common Jokers on blind select)

# #68 j_vampire — scaling X0.1 per Enhanced card played
def _vampire(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=1.3)

# #69 j_shortcut — utility (straights with 1-rank gaps). Needs evaluator change.

# #70 j_hologram — scaling X0.25 per card added to deck
def _hologram(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=1.5)

# #71 j_vagabond — utility (Tarot if $4 or less)

# #72 j_baron — X1.5 per King held in hand
def _baron(ctx: ScoreContext, j: dict) -> None:
    kings = sum(1 for c in ctx.held_cards if card_rank(c) == "K")
    if kings > 0:
        ctx.mult *= _ability(j).get("extra", 1.5) ** kings

# #73 j_cloud_9 — economy ($1 per 9 in deck)

# #74 j_rocket — economy ($1/round, +$2 after boss)

# #75 j_obelisk — scaling X0.2 per hand without most-played type
def _obelisk(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=1.6)

# #76 j_midas_mask — utility (face cards become Gold)

# #77 j_luchador — utility (sell to disable Boss Blind)

# #78 j_photograph — X2 for first scored face card
def _photograph(ctx: ScoreContext, j: dict) -> None:
    for c in ctx.scoring_cards:
        if not is_debuffed(c) and (ctx.pareidolia or card_rank(c) in FACE_RANKS):
            ctx.mult *= _ability(j).get("extra", 2.0)
            break

# #79 j_gift — economy (+$1 sell value to all)

# #80 j_turtle_bean — utility (+5 hand size, decays)

# #81 j_erosion — +4 Mult per card below starting deck size
def _erosion(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ab_mult(j, fallback=8)

# #82 j_reserved_parking — economy (1/2 chance $1 per held face card)

# #83 j_mail — economy ($5 per discarded target rank)

# #84 j_to_the_moon — economy (+$1 interest per $5)

# #85 j_hallucination — utility (1/2 Tarot on pack open)

# #86 j_fortune_teller — +1 Mult per Tarot used this run
def _fortune_teller(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ab_mult(j, fallback=3)

# #87 j_juggler — utility (+1 hand size)

# #88 j_drunkard — utility (+1 discard)

# #89 j_stone — +25 Chips per Stone Card in deck
def _stone_joker(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ab_chips(j, fallback=25)

# #90 j_golden — economy ($4 per round)

# #91 j_lucky_cat — scaling X0.25 per Lucky trigger
def _lucky_cat(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=1.5)

# #92 j_baseball — Uncommon Jokers each give X1.5
def _baseball(ctx: ScoreContext, j: dict) -> None:
    xm = _ability(j).get("extra", 1.5)
    uncommon_count = sum(
        1 for other in ctx.jokers
        if other is not j and other.get("value", {}).get("rarity") == 2
    )
    if uncommon_count > 0:
        ctx.mult *= xm ** uncommon_count

# #93 j_bull — +2 Chips per dollar
def _bull(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ability(j).get("extra", 2) * ctx.money

# #94 j_diet_cola — utility (sell for Double Tag)

# #95 j_trading — economy (destroy single discard for $3)

# #96 j_flash — scaling +2 Mult per reroll
def _flash(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ab_mult(j, fallback=6)

# #97 j_popcorn — +20 Mult, -4/round
def _popcorn(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ab_mult(j, fallback=12)

# #98 j_trousers — scaling +2 Mult per Two Pair
def _trousers(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ab_mult(j, fallback=6)

# #99 j_ancient — X1.5 per played card of rotating suit
def _ancient(ctx: ScoreContext, j: dict) -> None:
    if ctx.ancient_suit:
        # API provides the actual rotating suit — count exact matches
        count = sum(
            retrigger_count(c, ctx)
            for c in ctx.scoring_cards
            if not is_debuffed(c) and ctx.ancient_suit in card_suits(c)
        )
        if count > 0:
            ctx.mult *= 1.5 ** count
    else:
        # Fallback estimate: ~1-2 cards match = X1.5^1.5
        ctx.mult *= 2.0

# #100 j_ramen — X2, -X0.01 per card discarded
def _ramen(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=1.5)

# #101 j_walkie_talkie — +10 Chips +4 Mult per scored 10/4
def _walkie_talkie(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) in ("T", "4"))
    ctx.chips += ab.get("chips", 10) * count
    ctx.mult += ab.get("mult", 4) * count

# #102 j_selzer — retrigger all for 10 hands (handled in retrigger_count)

# #103 j_castle — scaling +3 Chips per discarded suit card
def _castle(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ab_chips(j, fallback=15)

# #104 j_smiley — +5 Mult per scored face card
def _smiley(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("extra", 5) * _count_face_in_scoring(ctx)

# #105 j_campfire — scaling X0.25 per sell, resets at boss
def _campfire(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=1.5)

# #106 j_ticket — economy ($4 per scored Gold card)

# #107 j_mr_bones — utility (prevents death at 25%)

# #108 j_acrobat — X3 on final hand
def _acrobat(ctx: ScoreContext, j: dict) -> None:
    if ctx.hands_left == 1:
        ctx.mult *= _ability(j).get("extra", 3.0)

# #109 j_sock_and_buskin — retrigger face cards (handled in retrigger_count)

# #110 j_swashbuckler — Mult = total sell value of other jokers
def _swashbuckler(ctx: ScoreContext, j: dict) -> None:
    total_sell = sum(
        other.get("cost", {}).get("sell", 0)
        for other in ctx.jokers if other is not j
    )
    ctx.mult += total_sell

# #111 j_troubadour — utility (+2 hand size, -1 hand)

# #112 j_certificate — utility (random sealed card on round start)

# #113 j_smeared — utility (red/black suit merging). Needs evaluator change.

# #114 j_throwback — scaling X0.25 per blind skipped
def _throwback(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=1.5)

# #115 j_hanging_chad — retrigger first scored card 2x (handled in retrigger_count)

# #116 j_rough_gem — economy ($1 per scored Diamond)

# #117 j_bloodstone — 1/2 chance X1.5 per scored Heart (probabilistic, must estimate)
def _bloodstone(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    xm = ab.get("Xmult", 1.5)
    odds = ab.get("odds", 2)  # 1 in N chance
    hearts = _count_suit_in_scoring(ctx, "H")
    # Expected value: xm^(triggers * (1/odds))
    if hearts > 0:
        ctx.mult *= xm ** (hearts * (1.0 / odds))

# #118 j_arrowhead — +50 Chips per scored Spade
def _arrowhead(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ability(j).get("extra", 50) * _count_suit_in_scoring(ctx, "S")

# #119 j_onyx_agate — +7 Mult per scored Club
def _onyx_agate(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("extra", 7) * _count_suit_in_scoring(ctx, "C")

# #120 j_glass — scaling X0.75 per Glass Card destroyed
def _glass(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=1.5)

# #121 j_ring_master (Showman) — utility (cards can appear multiple times)

# #122 j_flower_pot — X3 if all 4 suits in hand
def _flower_pot(ctx: ScoreContext, j: dict) -> None:
    # Checks all played cards, not just scoring cards — the non-scoring kickers
    # can provide the missing suit(s) that complete the four-suit requirement.
    suits_present: set[str] = set()
    for c in ctx.played_cards:
        if not is_debuffed(c):
            suits_present |= card_suits(c)
    if len(suits_present) >= 4:
        ctx.mult *= _ability(j).get("extra", 3.0)

# #123 j_blueprint — copies right joker's ability
def _blueprint(ctx: ScoreContext, j: dict) -> None:
    # Find joker to the right of blueprint
    for i, jk in enumerate(ctx.jokers):
        if jk is j and i + 1 < len(ctx.jokers):
            right = ctx.jokers[i + 1]
            effect = JOKER_EFFECTS.get(right.get("key", ""))
            if effect and effect is not _blueprint and effect is not _brainstorm:
                effect(ctx, right)
            break

# #124 j_wee — scaling +8 Chips per scored 2
def _wee(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ab_chips(j, fallback=16)

# #125 j_merry_andy — utility (+3 discards, -1 hand size)

# #126 j_oops — utility (double all probabilities)

# #127 j_idol — X2 for specific rotating card rank+suit
def _idol(ctx: ScoreContext, j: dict) -> None:
    # TODO: API could expose the target card. For now, estimate 20% match.
    ctx.mult *= _ability(j).get("extra", 2.0) ** 0.2

# #128 j_seeing_double — X2 if Club + other suit
def _seeing_double(ctx: ScoreContext, j: dict) -> None:
    # Checks all played cards — non-scoring kickers can supply the Club or other suit.
    has_club  = any("C" in card_suits(c) for c in ctx.scoring_cards if not is_debuffed(c))
    has_other = any(card_suits(c) - {"C"} for c in ctx.scoring_cards if not is_debuffed(c))
    if has_club and has_other:
        ctx.mult *= _ability(j).get("extra", 2.0)

# #129 j_matador — economy ($8 if triggering Boss Blind)

# #130 j_hit_the_road — scaling X0.5 per Jack discarded this round
def _hit_the_road(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=1.5)

# #131 j_duo — X2 if Pair
def _duo(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Pair"):
        ctx.mult *= _ab_xmult(j, fallback=2.0)

# #132 j_trio — X3 if Three of a Kind
def _trio(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Three of a Kind"):
        ctx.mult *= _ab_xmult(j, fallback=3.0)

# #133 j_family — X4 if Four of a Kind
def _family(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Four of a Kind"):
        ctx.mult *= _ab_xmult(j, fallback=4.0)

# #134 j_order — X3 if Straight
def _order(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Straight"):
        ctx.mult *= _ab_xmult(j, fallback=3.0)

# #135 j_tribe — X2 if Flush
def _tribe(ctx: ScoreContext, j: dict) -> None:
    if _hand_contains(ctx, "Flush"):
        ctx.mult *= _ab_xmult(j, fallback=2.0)

# #136 j_stuntman — +250 Chips, -2 hand size
def _stuntman(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ability(j).get("chip_mod", 250)

# #137 j_invisible — utility (sell after 2 rounds to dupe joker)

# #138 j_brainstorm — copies leftmost joker's ability
def _brainstorm(ctx: ScoreContext, j: dict) -> None:
    if ctx.jokers:
        left = ctx.jokers[0]
        if left is not j:
            effect = JOKER_EFFECTS.get(left.get("key", ""))
            if effect and effect is not _brainstorm and effect is not _blueprint:
                effect(ctx, left)

# #139 j_satellite — economy ($1 per unique Planet used)

# #140 j_shoot_the_moon — +13 Mult per Queen held in hand
def _shoot_the_moon(ctx: ScoreContext, j: dict) -> None:
    queens = sum(1 for c in ctx.held_cards if card_rank(c) == "Q")
    ctx.mult += _ability(j).get("extra", 13) * queens

# #141 j_drivers_license — X3 if 16+ Enhanced cards in deck
def _drivers_license(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    tally = ab.get("driver_tally")
    if tally is not None:
        if tally >= 16:
            ctx.mult *= ab.get("extra", 3.0)
    else:
        # No tally from API — don't apply (can't verify condition)
        pass

# #142 j_cartomancer — utility (Tarot on blind select)

# #143 j_astronomer — economy (free Planet cards)

# #144 j_burnt — utility (upgrade first discarded hand level)

# #145 j_bootstraps — +2 Mult per $5
def _bootstraps(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    ctx.mult += ab.get("mult", 2) * (ctx.money // ab.get("dollars", 5))

# #146 j_canio — scaling X1 per face card destroyed
def _canio(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=2.0)

# #147 j_triboulet — X2 per scored King or Queen
def _triboulet(ctx: ScoreContext, j: dict) -> None:
    xm = _ability(j).get("extra", 2.0)
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) in ("K", "Q"))
    if count > 0:
        ctx.mult *= xm ** count

# #148 j_yorick — scaling X1 per 23 cards discarded
def _yorick(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ab_xmult(j, fallback=2.0)

# #149 j_chicot — utility (disable all Boss Blinds)

# #150 j_perkeo — utility (Negative copy of random consumable)


# ---------------------------------------------------------------------------
# Registry — all 150 jokers
# ---------------------------------------------------------------------------

JOKER_EFFECTS: dict[str, Callable[[ScoreContext, dict], None]] = {
    # --- Scoring jokers ---
    # Unconditional
    "j_joker": _joker,                      # #1
    "j_misprint": _misprint,                # #27
    "j_gros_michel": _gros_michel,          # #38
    "j_popcorn": _popcorn,                  # #97
    "j_ice_cream": _ice_cream,              # #50
    "j_cavendish": _cavendish,              # #61
    "j_blue_joker": _blue_joker,            # #53
    "j_stuntman": _stuntman,                # #136

    # Hand-type +mult
    "j_jolly": _jolly,                      # #6
    "j_zany": _zany,                        # #7
    "j_mad": _mad,                          # #8
    "j_crazy": _crazy,                      # #9
    "j_droll": _droll,                      # #10

    # Hand-type +chips
    "j_sly": _sly,                          # #11
    "j_wily": _wily,                        # #12
    "j_clever": _clever,                    # #13
    "j_devious": _devious,                  # #14
    "j_crafty": _crafty,                    # #15

    # Hand-type xmult
    "j_duo": _duo,                          # #131
    "j_trio": _trio,                        # #132
    "j_family": _family,                    # #133
    "j_order": _order,                      # #134
    "j_tribe": _tribe,                      # #135

    # Suit conditional
    "j_greedy_joker": _greedy,              # #2
    "j_lusty_joker": _lusty,               # #3
    "j_wrathful_joker": _wrathful,         # #4
    "j_gluttenous_joker": _gluttenous,     # #5
    "j_arrowhead": _arrowhead,              # #118
    "j_onyx_agate": _onyx_agate,            # #119

    # Card property
    "j_scary_face": _scary_face,            # #33
    "j_smiley": _smiley,                    # #104
    "j_fibonacci": _fibonacci,              # #31
    "j_even_steven": _even_steven,          # #39
    "j_odd_todd": _odd_todd,                # #40
    "j_scholar": _scholar,                  # #41
    "j_walkie_talkie": _walkie_talkie,      # #101
    "j_photograph": _photograph,            # #78

    # Game state
    "j_half": _half,                        # #16
    "j_stencil": _stencil,                  # #17
    "j_abstract": _abstract,                # #34
    "j_banner": _banner,                    # #22
    "j_mystic_summit": _mystic_summit,      # #23
    "j_bull": _bull,                        # #93
    "j_bootstraps": _bootstraps,            # #145
    "j_blackboard": _blackboard,            # #48
    "j_acrobat": _acrobat,                  # #108
    "j_seeing_double": _seeing_double,      # #128
    "j_flower_pot": _flower_pot,            # #122
    "j_shoot_the_moon": _shoot_the_moon,    # #140
    "j_raised_fist": _raised_fist,          # #29
    "j_swashbuckler": _swashbuckler,        # #110
    "j_erosion": _erosion,                  # #81
    "j_baron": _baron,                      # #72
    "j_bloodstone": _bloodstone,            # #117
    "j_triboulet": _triboulet,              # #147

    # Scaling (estimated mid-run values)
    "j_ceremonial": _ceremonial,            # #21
    "j_loyalty_card": _loyalty_card,        # #25
    "j_ride_the_bus": _ride_the_bus,        # #44
    "j_supernova": _supernova,              # #43
    "j_green_joker": _green_joker,          # #58
    "j_red_card": _red_card,                # #63
    "j_fortune_teller": _fortune_teller,    # #86
    "j_flash": _flash,                      # #96
    "j_trousers": _trousers,                # #98
    "j_runner": _runner,                    # #49
    "j_square": _square,                    # #65
    "j_castle": _castle,                    # #103
    "j_wee": _wee,                          # #124
    "j_hiker": _hiker,                      # #56
    "j_constellation": _constellation,      # #55
    "j_madness": _madness,                  # #64
    "j_vampire": _vampire,                  # #68
    "j_hologram": _hologram,                # #70
    "j_obelisk": _obelisk,                  # #75
    "j_ramen": _ramen,                      # #100
    "j_lucky_cat": _lucky_cat,              # #91
    "j_glass": _glass,                      # #120
    "j_hit_the_road": _hit_the_road,        # #130
    "j_campfire": _campfire,                # #105
    "j_throwback": _throwback,              # #114
    "j_card_sharp": _card_sharp,            # #62
    "j_ancient": _ancient,                  # #99
    "j_steel_joker": _steel_joker,          # #32
    "j_stone": _stone_joker,                # #89
    "j_idol": _idol,                        # #127
    "j_drivers_license": _drivers_license,  # #141
    "j_baseball": _baseball,                # #92
    "j_canio": _canio,                      # #146
    "j_yorick": _yorick,                    # #148

    # Copy jokers
    "j_blueprint": _blueprint,              # #123
    "j_brainstorm": _brainstorm,            # #138

    # --- Retrigger jokers (scoring handled in retrigger_count) ---
    "j_hack": _noop,                        # #36
    "j_dusk": _noop,                        # #28
    "j_sock_and_buskin": _noop,             # #109
    "j_hanging_chad": _noop,                # #115
    "j_selzer": _noop,                      # #102
    "j_mime": _noop,                        # #19

    # --- Economy jokers (no scoring effect, but recognized as "known") ---
    "j_credit_card": _noop,                 # #20
    "j_chaos": _noop,                       # #30
    "j_delayed_grat": _noop,                # #35
    "j_business": _noop,                    # #42
    "j_egg": _noop,                         # #46
    "j_faceless": _noop,                    # #57
    "j_todo_list": _noop,                   # #60
    "j_cloud_9": _noop,                     # #73
    "j_rocket": _noop,                      # #74
    "j_gift": _noop,                        # #79
    "j_reserved_parking": _noop,            # #82
    "j_mail": _noop,                        # #83
    "j_to_the_moon": _noop,                 # #84
    "j_golden": _noop,                      # #90
    "j_trading": _noop,                     # #95
    "j_ticket": _noop,                      # #106
    "j_rough_gem": _noop,                   # #116
    "j_matador": _noop,                     # #129
    "j_satellite": _noop,                   # #139
    "j_astronomer": _noop,                  # #143

    # --- Utility jokers (no direct scoring, but recognized as "known") ---
    "j_four_fingers": _noop,                # #18 — needs evaluator change
    "j_marble": _noop,                      # #24
    "j_8_ball": _noop,                      # #26
    "j_space": _noop,                       # #45
    "j_burglar": _noop,                     # #47
    "j_dna": _noop,                         # #51
    "j_splash": _noop,                      # #52 — needs evaluator change
    "j_sixth_sense": _noop,                 # #54
    "j_superposition": _noop,               # #59
    "j_shortcut": _noop,                    # #69 — needs evaluator change
    "j_seance": _noop,                      # #66
    "j_riff_raff": _noop,                   # #67
    "j_vagabond": _noop,                    # #71
    "j_midas_mask": _noop,                  # #76
    "j_luchador": _noop,                    # #77
    "j_turtle_bean": _noop,                 # #80
    "j_hallucination": _noop,               # #85
    "j_juggler": _noop,                     # #87
    "j_drunkard": _noop,                    # #88
    "j_diet_cola": _noop,                   # #94
    "j_mr_bones": _noop,                    # #107
    "j_pareidolia": _noop,                  # #37 — needs evaluator change
    "j_troubadour": _noop,                  # #111
    "j_certificate": _noop,                 # #112
    "j_smeared": _noop,                     # #113 — needs evaluator change
    "j_ring_master": _noop,                 # #121
    "j_merry_andy": _noop,                  # #125
    "j_oops": _noop,                        # #126
    "j_invisible": _noop,                   # #137
    "j_burnt": _noop,                       # #144
    "j_cartomancer": _noop,                 # #142
    "j_chicot": _noop,                      # #149
    "j_perkeo": _noop,                      # #150
}


def apply_joker_effects(ctx: ScoreContext) -> None:
    """Apply all owned joker effects to the scoring context, in order."""
    for joker in ctx.jokers:
        key = joker.get("key", "")
        effect = JOKER_EFFECTS.get(key)
        if effect is not None:
            effect(ctx, joker)


def apply_joker_effects_detailed(ctx: ScoreContext) -> list[tuple[str, float, float]]:
    """Like apply_joker_effects, but returns per-joker (label, delta_chips, delta_mult).

    Only call this for hands actually played — not in enumerate_hands.
    """
    contributions = []
    for joker in ctx.jokers:
        key = joker.get("key", "")
        label = joker.get("label", key)
        effect = JOKER_EFFECTS.get(key)
        if effect is not None:
            pre_chips, pre_mult = ctx.chips, ctx.mult
            effect(ctx, joker)
            contributions.append((
                label,
                ctx.chips - pre_chips,
                ctx.mult - pre_mult,
            ))
    return contributions
