"""Complex joker effects — hand-written functions for non-trivial logic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.cards import card_rank, card_suits, is_debuffed, _modifier, rank_value
from balatro_bot.constants import FACE_RANKS, FIBONACCI_RANKS, EVEN_RANKS, ODD_RANKS
from balatro_bot.joker_effects.parsers import _ability, _ab_chips, _ab_mult, _ab_xmult
from balatro_bot.joker_effects.context import ScoreContext, retrigger_count, _count_suit_in_scoring, _count_face_in_scoring, _hand_contains

if TYPE_CHECKING:
    from typing import Any


# --- Conditional flat additions ---

def _half(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    if len(ctx.played_cards) <= ab.get("size", 3):
        ctx.mult += ab.get("mult", 20)

def _stencil(ctx: ScoreContext, j: dict) -> None:
    empty = (ctx.joker_limit - len(ctx.jokers)) + 1
    ctx.mult *= max(1, empty)

def _banner(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ability(j).get("extra", 30) * ctx.discards_left

def _mystic_summit(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    if ctx.discards_left <= ab.get("d_remaining", 0):
        ctx.mult += ab.get("mult", 15)

def _loyalty_card(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    remaining = ab.get("loyalty_remaining")
    if remaining is not None:
        if remaining == 0:
            ctx.mult *= ab.get("Xmult", 4)
    else:
        ctx.mult *= 1.5

def _misprint(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    lo = ab.get("min", 0)
    hi = ab.get("max", 23)
    ctx.mult += (lo + hi) / 2

def _raised_fist(ctx: ScoreContext, j: dict) -> None:
    held_ranks = [rank_value(card_rank(c)) for c in ctx.held_cards if card_rank(c)]
    if held_ranks:
        ctx.mult += 2 * min(held_ranks)

def _fibonacci(ctx: ScoreContext, j: dict) -> None:
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) in FIBONACCI_RANKS)
    ctx.mult += _ability(j).get("extra", 8) * count

def _scary_face(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ability(j).get("extra", 30) * _count_face_in_scoring(ctx)

def _abstract(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("extra", 3) * len(ctx.jokers)

def _even_steven(ctx: ScoreContext, j: dict) -> None:
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) in EVEN_RANKS)
    ctx.mult += _ability(j).get("extra", 4) * count

def _odd_todd(ctx: ScoreContext, j: dict) -> None:
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) in ODD_RANKS)
    ctx.chips += _ability(j).get("extra", 31) * count

def _scholar(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) == "A")
    ctx.chips += ab.get("chips", 20) * count
    ctx.mult += ab.get("mult", 4) * count

def _supernova(ctx: ScoreContext, j: dict) -> None:
    played = ctx.hand_levels.get(ctx.hand_name, {}).get("played", 0) + 1
    ctx.mult += played

def _green_joker(ctx: ScoreContext, j: dict) -> None:
    """Green Joker increments in context.before (before scoring), so add hand_add."""
    ab = _ability(j)
    ctx.mult += _ab_mult(j, fallback=5) + ab.get("hand_add", 1)

def _ride_the_bus(ctx: ScoreContext, j: dict) -> None:
    """Ride the Bus: +extra per hand without face cards. Resets on face cards.
    Increments in context.before, so snapshot is stale by +extra when no faces."""
    ab = _ability(j)
    base = _ab_mult(j, fallback=5)
    has_face = any(card_rank(c) in FACE_RANKS for c in ctx.scoring_cards if card_rank(c))
    if has_face:
        # Resets to 0 in context.before — game scores 0
        pass
    else:
        # Gains +extra in context.before, then scores the new total
        ctx.mult += base + ab.get("extra", 1)

def _blackboard(ctx: ScoreContext, j: dict) -> None:
    if ctx.held_cards and all(
        card_suits(c) & {"S", "C"} for c in ctx.held_cards if card_suits(c)
    ):
        ctx.mult *= _ability(j).get("extra", 3.0)

def _baron(ctx: ScoreContext, j: dict) -> None:
    kings = sum(1 for c in ctx.held_cards if card_rank(c) == "K")
    if kings > 0:
        ctx.mult *= _ability(j).get("extra", 1.5) ** kings

def _photograph(ctx: ScoreContext, j: dict) -> None:
    for c in ctx.scoring_cards:
        if not is_debuffed(c) and (ctx.pareidolia or card_rank(c) in FACE_RANKS):
            ctx.mult *= _ability(j).get("extra", 2.0)
            break

def _smiley(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("extra", 5) * _count_face_in_scoring(ctx)

def _acrobat(ctx: ScoreContext, j: dict) -> None:
    if ctx.hands_left == 1:
        ctx.mult *= _ability(j).get("extra", 3.0)

def _card_sharp(ctx: ScoreContext, j: dict) -> None:
    played_count = ctx.hand_levels.get(ctx.hand_name, {}).get("played_this_round", 0)
    if played_count > 0:
        ctx.mult *= _ab_xmult(j, fallback=3.0)

def _ancient(ctx: ScoreContext, j: dict) -> None:
    if ctx.ancient_suit:
        count = sum(
            retrigger_count(c, ctx)
            for c in ctx.scoring_cards
            if not is_debuffed(c) and ctx.ancient_suit in card_suits(c)
        )
        if count > 0:
            ctx.mult *= 1.5 ** count
    else:
        ctx.mult *= 2.0

def _walkie_talkie(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) in ("T", "4"))
    ctx.chips += ab.get("chips", 10) * count
    ctx.mult += ab.get("mult", 4) * count

def _seeing_double(ctx: ScoreContext, j: dict) -> None:
    has_club  = any("C" in card_suits(c) for c in ctx.scoring_cards if not is_debuffed(c))
    has_other = any(card_suits(c) - {"C"} for c in ctx.scoring_cards if not is_debuffed(c))
    if has_club and has_other:
        ctx.mult *= _ability(j).get("extra", 2.0)

def _flower_pot(ctx: ScoreContext, j: dict) -> None:
    # Game checks scoring_hand, not all played cards. Only scoring cards
    # contribute suits toward the 4-suit requirement.
    suits_present: set[str] = set()
    for c in ctx.scoring_cards:
        if not is_debuffed(c):
            suits_present |= card_suits(c)
    if len(suits_present) >= 4:
        ctx.mult *= _ability(j).get("extra", 3.0)

def _blueprint(ctx: ScoreContext, j: dict) -> None:
    # Import JOKER_EFFECTS lazily to avoid circular import
    from balatro_bot.joker_effects.registry import JOKER_EFFECTS
    for i, jk in enumerate(ctx.jokers):
        if jk is j and i + 1 < len(ctx.jokers):
            right = ctx.jokers[i + 1]
            effect = JOKER_EFFECTS.get(right.get("key", ""))
            if effect and effect is not _blueprint and effect is not _brainstorm:
                effect(ctx, right)
            break

def _brainstorm(ctx: ScoreContext, j: dict) -> None:
    from balatro_bot.joker_effects.registry import JOKER_EFFECTS
    if ctx.jokers:
        left = ctx.jokers[0]
        if left is not j:
            effect = JOKER_EFFECTS.get(left.get("key", ""))
            if effect and effect is not _brainstorm and effect is not _blueprint:
                effect(ctx, left)

def _shoot_the_moon(ctx: ScoreContext, j: dict) -> None:
    queens = sum(1 for c in ctx.held_cards if card_rank(c) == "Q")
    ctx.mult += _ability(j).get("extra", 13) * queens

def _drivers_license(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    tally = ab.get("driver_tally")
    if tally is not None:
        if tally >= 16:
            ctx.mult *= ab.get("extra", 3.0)

def _bootstraps(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    ctx.mult += ab.get("mult", 2) * (ctx.money // ab.get("dollars", 5))

def _swashbuckler(ctx: ScoreContext, j: dict) -> None:
    total_sell = sum(
        other.get("cost", {}).get("sell", 0)
        for other in ctx.jokers if other is not j
    )
    ctx.mult += total_sell

def _bloodstone(ctx: ScoreContext, j: dict) -> None:
    ab = _ability(j)
    xm = ab.get("Xmult", 1.5)
    odds = ab.get("odds", 2)
    hearts = _count_suit_in_scoring(ctx, "H")
    if hearts > 0:
        ctx.mult *= xm ** (hearts * (1.0 / odds))

def _arrowhead(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ability(j).get("extra", 50) * _count_suit_in_scoring(ctx, "S")

def _onyx_agate(ctx: ScoreContext, j: dict) -> None:
    ctx.mult += _ability(j).get("extra", 7) * _count_suit_in_scoring(ctx, "C")

def _triboulet(ctx: ScoreContext, j: dict) -> None:
    xm = _ability(j).get("extra", 2.0)
    count = sum(retrigger_count(c, ctx) for c in ctx.scoring_cards if not is_debuffed(c) and card_rank(c) in ("K", "Q"))
    if count > 0:
        ctx.mult *= xm ** count

def _baseball(ctx: ScoreContext, j: dict) -> None:
    xm = _ability(j).get("extra", 1.5)
    uncommon_count = sum(
        1 for other in ctx.jokers
        if other is not j and other.get("value", {}).get("rarity") == 2
    )
    if uncommon_count > 0:
        ctx.mult *= xm ** uncommon_count

def _bull(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ability(j).get("extra", 2) * ctx.money

def _stuntman(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ability(j).get("chip_mod", 250)

def _idol(ctx: ScoreContext, j: dict) -> None:
    ctx.mult *= _ability(j).get("extra", 2.0) ** 0.2

def _wee(ctx: ScoreContext, j: dict) -> None:
    ctx.chips += _ab_chips(j, fallback=16)


# Collected dict of complex effects for the registry
COMPLEX_EFFECTS: dict[str, object] = {
    "j_half": _half,
    "j_stencil": _stencil,
    "j_banner": _banner,
    "j_mystic_summit": _mystic_summit,
    "j_loyalty_card": _loyalty_card,
    "j_misprint": _misprint,
    "j_raised_fist": _raised_fist,
    "j_fibonacci": _fibonacci,
    "j_scary_face": _scary_face,
    "j_abstract": _abstract,
    "j_even_steven": _even_steven,
    "j_odd_todd": _odd_todd,
    "j_scholar": _scholar,
    "j_supernova": _supernova,
    "j_green_joker": _green_joker,
    "j_ride_the_bus": _ride_the_bus,
    "j_blackboard": _blackboard,
    "j_baron": _baron,
    "j_photograph": _photograph,
    "j_smiley": _smiley,
    "j_acrobat": _acrobat,
    "j_card_sharp": _card_sharp,
    "j_ancient": _ancient,
    "j_walkie_talkie": _walkie_talkie,
    "j_seeing_double": _seeing_double,
    "j_flower_pot": _flower_pot,
    "j_blueprint": _blueprint,
    "j_brainstorm": _brainstorm,
    "j_shoot_the_moon": _shoot_the_moon,
    "j_drivers_license": _drivers_license,
    "j_bootstraps": _bootstraps,
    "j_swashbuckler": _swashbuckler,
    "j_bloodstone": _bloodstone,
    "j_arrowhead": _arrowhead,
    "j_onyx_agate": _onyx_agate,
    "j_triboulet": _triboulet,
    "j_baseball": _baseball,
    "j_bull": _bull,
    "j_stuntman": _stuntman,
    "j_idol": _idol,
    "j_wee": _wee,
}
