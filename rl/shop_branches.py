"""Shop branches: buys and uses a copy of the bot under-tries, each tried on a branch so
the result teaches the policy what it is worth (``rl.branch``).

A proposal is the moves the branch makes in place of the main line's own (a sale or two,
then the buy), or, for a planet bought for a hand the run does not play, a joker in
``PASSED_OVER`` or a joker or voucher that conflicts with a held joker or owned voucher,
the card the branch may not buy this shop visit. Joker pairs and conflicts are the rule
bot's own tables; only which jokers go together is used, not the bot's prices for them.
Voucher pairs and conflicts (``VOUCHER_PAIRS``, ``VOUCHER_CONFLICTS``) work either way
round: a voucher on sale beside a held joker, or a joker on sale beside an owned voucher.
Jokers are judged by what Tyler asked of them: does
the trade raise the average score of the best play over hands dealt from the run's deck
(``hand_score``)? The test only decides whether a trade is tried; the branch's result is
what the policy learns from. Scaling and money jokers score little up front, so the test
passes them over, as the policy does now.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cache
from itertools import combinations

import numpy as np

from jackdaw.core.game import IllegalActionError
from jackdaw.engine import actions as A
from jackdaw.engine.data import load
from jackdaw.env.action_space import ActionType
from jackdaw.env.game_spec import FactoredAction

import rl.ordering  # noqa: F401  (puts the bot's src on the path)
from balatro_bot.scaling import (  # noqa: E402
    AMPLIFICATION_PAIRS, ANTI_SYNERGY, SELL_PROTECTED, VOUCHER_CONFLICTS, VOUCHER_PAIRS,
)

BUY, BUY_AND_USE = int(ActionType.BuyCard), int(ActionType.BuyAndUse)
REDEEM = int(ActionType.RedeemVoucher)
SELL_JOKER, SELL_CONS = int(ActionType.SellJoker), int(ActionType.SellConsumable)
USE, LEAVE = int(ActionType.UseConsumable), int(ActionType.NextRound)
SWAP_LEFT, SWAP_RIGHT = int(ActionType.SwapJokersLeft), int(ActionType.SwapJokersRight)
PICK = int(ActionType.PickPackCard)
RED_SEAL = "Red"
"""Tyler: seals are very strong, and a Red Seal (the card retriggers) is the best of them."""

_CENTERS = load("centers")
ENHANCING_TAROTS = frozenset(
    k for k, c in _CENTERS.items()
    if c.get("set") == "Tarot" and str((c.get("config") or {}).get("mod_conv", "")).startswith("m_"))
"""Tarots that enhance the cards they target (Magician, Empress, Lovers, Devil, ...)."""
PLANET_HAND = {k: c["config"]["hand_type"] for k, c in _CENTERS.items()
               if c.get("set") == "Planet" and "hand_type" in (c.get("config") or {})}
COPY_JOKERS = frozenset({"j_blueprint", "j_brainstorm"})
MIDAS, VAMPIRE = "j_midas_mask", "j_vampire"
MADNESS = "j_madness"
"""Destroys a random joker at each non-boss blind, so an open slot beside it wants fodder."""
PASSED_OVER = frozenset({"j_campfire"})
"""Jokers the policy buys more than they are worth (Tyler: Campfire kind of sucks); a
branch passes on the buy."""
PAIRS: list[tuple[str, frozenset[str]]] = [(e, b) for e, b, _ in AMPLIFICATION_PAIRS]
"""The rule bot's amplification pairs: an enabler and the jokers it boosts, either way round."""
CONFLICTS: dict[str, frozenset[str]] = ANTI_SYNERGY
"""The rule bot's anti-synergy map: a joker and the held jokers it works against."""

KINDS = ("copy joker", "midas", "synergy", "voucher synergy", "pass joker", "conflict",
         "best joker", "fill slot", "off-build planet", "held tarot", "red seal", "upgraded card")
"""Tried in this order; a main line takes at most one branch per tick."""
SHOP_CHANCE: dict[str, float] = {
    "copy joker": 1.0, "midas": 1.0, "synergy": 0.2, "voucher synergy": 0.2,
    "pass joker": 1.0, "conflict": 1.0,
    "best joker": 0.1, "fill slot": 0.25, "off-build planet": 0.25, "held tarot": 0.25,
    "red seal": 1.0, "upgraded card": 0.5,
}
"""The chance each branch is tried when it comes up: once per shop visit, a held tarot
once per blind, a playing card pick once per pick."""
LATE_ANTE = 6
LATE_CHANCE: dict[str, float] = {"best joker": 0.2}
"""Chances from ``LATE_ANTE`` on. Tyler: jokers like Smiley Face clear blinds early but
fall off; past ante 6 you look to swap them for something better, which is the trade the
best-joker branch tries."""


def chance(kind: str, ante: int) -> float:
    """The chance a ``kind`` branch is tried at ``ante``."""
    return LATE_CHANCE.get(kind, SHOP_CHANCE[kind]) if ante >= LATE_ANTE else SHOP_CHANCE[kind]
HANDS = 10
"""Hands dealt from the run's deck to score a lineup on."""
GAIN = 0.10
"""How much a joker must raise the average best-hand score for a best-joker branch."""
OFF_BUILD = 0.05
"""A planet is off-build when its hand type is under this share of the run's plays..."""
MIN_PLAYS = 10
"""...once the run has played this many hands."""


@dataclass
class Proposal:
    kind: str
    moves: list[FactoredAction]
    """The branch's moves in order; empty for a veto."""
    veto: int | None = None
    """The sort id of a shop card the branch may not buy this visit."""


# -- scoring a lineup ---------------------------------------------------------------


def dealt_hands(game, state, rng: np.random.Generator, n: int = HANDS) -> list[list[int]]:
    """``n`` hands of the run's hand size, as positions in its deck sorted by sort id.
    Hand-size penalties can push the size below zero; such hands are empty and score 0."""
    size = len(game.deck) + len(game.hand) + len(game.discard_pile)
    k = max(0, min(int(state.hand_size), size))
    return [sorted(rng.choice(size, k, replace=False).tolist()) for _ in range(n)]


def hand_score(game, hands: list[list[int]], moves=()) -> float:
    """The average score of the best play over ``hands`` on a copy of ``game``, after the
    shop ``moves`` and the start of the next blind. A boss there is disabled on the copy:
    the test is of the lineup, not of the boss."""
    c = game.clone()
    for m in moves:
        c.step(_engine(m))
    c.step(A.NextRound())
    c.step(A.SelectBlind())
    if c.blind.boss:
        c.debug_set_blind_disabled(True)
    ids = sorted(x.sort_id for x in (*c.hand, *c.deck, *c.discard_pile))
    total = 0.0
    for hand in hands:
        c.debug_set_hand([ids[i] for i in hand if i < len(ids)])
        c.debug_clear_forced_selection()
        n = len(c.hand)
        total += max((_score(c, s) for k in range(1, min(5, n) + 1)
                      for s in combinations(range(n), k)), default=0.0)
    return total / max(len(hands), 1)


def _score(game, cards) -> float:
    try:
        return game.preview_play(list(cards)).total
    except IllegalActionError:
        return 0.0


def _engine(m: FactoredAction):
    t = int(m.action_type)
    if t == BUY:
        return A.BuyCard(m.entity_target)
    if t == SELL_JOKER:
        return A.SellCard("jokers", m.entity_target)
    if t == SELL_CONS:
        return A.SellCard("consumables", m.entity_target)
    if t == REDEEM:
        return A.RedeemVoucher(m.entity_target)
    if t == SWAP_LEFT:
        return A.SwapJokersLeft(m.entity_target)
    if t == SWAP_RIGHT:
        return A.SwapJokersRight(m.entity_target)
    raise ValueError(f"not a shop move: {m}")


# -- buying, with sales if needed --------------------------------------------------------


def buy_plans(game, state, mask, shop_index: int,
              keep: frozenset[str] = frozenset()) -> list[list[FactoredAction]]:
    """The ways to buy shop card ``shop_index``: as is, or after selling at most one
    joker (never one in ``keep`` or ``SELL_PROTECTED``) and then consumables (from the
    last) until it is affordable. Each is checked on a copy of the game, so only ways the
    game takes are given; empty when none is."""
    keep = keep | SELL_PROTECTED
    card = game.shop_jokers[shop_index]
    sell_j = mask.entity_masks.get(ActionType.SellJoker)
    sell_c = mask.entity_masks.get(ActionType.SellConsumable)
    jokers = [i for i in range(len(game.jokers))
              if sell_j is not None and sell_j[i] and game.jokers[i].key not in keep]
    cons = [i for i in range(len(game.consumables)) if sell_c is not None and sell_c[i]]
    full = (card.key.startswith("j_") and card.edition != "negative"
            and len(game.jokers) >= int(state.joker_slots))
    plans = []
    for j in ([] if full else [None]) + jokers:
        money = float(game.dollars) + (game.jokers[j].sell_cost if j is not None else 0.0)
        moves = [] if j is None else [FactoredAction(SELL_JOKER, entity_target=j)]
        for i in reversed(cons):
            if money >= card.cost:
                break
            money += game.consumables[i].sell_cost
            moves.append(FactoredAction(SELL_CONS, entity_target=i))
        if money >= card.cost:
            plans.append(moves + [FactoredAction(BUY, entity_target=shop_index)])
    return [p for p in plans if _plays(game, p)]


def redeem_plan(game, mask, voucher_index: int) -> list[FactoredAction] | None:
    """Redeem shop voucher ``voucher_index``, after selling consumables (from the last)
    until it is affordable; never a joker. None when the game does not take it."""
    cost = game.shop_vouchers[voucher_index].cost
    sell_c = mask.entity_masks.get(ActionType.SellConsumable)
    money, moves = float(game.dollars), []
    for i in reversed(range(len(game.consumables))):
        if money >= cost:
            break
        if sell_c is not None and sell_c[i]:
            money += game.consumables[i].sell_cost
            moves.append(FactoredAction(SELL_CONS, entity_target=i))
    plan = moves + [FactoredAction(REDEEM, entity_target=voucher_index)]
    return plan if money >= cost and _plays(game, plan) else None


def _plays(game, moves) -> bool:
    """Whether the game takes ``moves`` in order (a copy is played; the game is unchanged)."""
    c = game.clone()
    try:
        for m in moves:
            c.step(_engine(m))
    except IllegalActionError:
        return False
    return True


def best_plan(game, plans, hands) -> list[FactoredAction]:
    """The plan whose lineup scores best; with one plan, that one unscored."""
    if len(plans) == 1:
        return plans[0]
    return max(plans, key=lambda p: hand_score(game, hands, p))


def _moved(at: int, to: int) -> list[FactoredAction]:
    """The joker swaps that move the joker at ``at`` to ``to``."""
    if to < at:
        return [FactoredAction(SWAP_LEFT, entity_target=i) for i in range(at, to, -1)]
    return [FactoredAction(SWAP_RIGHT, entity_target=i) for i in range(at, to)]


def placed(game, plan, hands, sort_id: int) -> list[FactoredAction]:
    """``plan`` followed by the joker swaps that set up the copy joker ``sort_id`` best by
    lineup score (no swaps on a tie). Blueprint copies the joker to its right, and a bought
    one lands on the right, where it copies nothing: it is moved to its best spot.
    Brainstorm copies the leftmost joker: the joker moved to the left end is chosen."""
    c = game.clone()
    for m in plan:
        c.step(_engine(m))
    at = next(i for i, j in enumerate(c.jokers) if j.sort_id == sort_id)
    if c.jokers[at].key == "j_brainstorm":
        moves = [_moved(i, 0) for i in range(1, len(c.jokers)) if i != at]
    else:
        moves = [_moved(at, to) for to in range(len(c.jokers)) if to != at]
    options = [plan] + [o for o in (plan + m for m in moves) if _plays(game, o)]
    if len(options) == 1:
        return plan
    return max(options, key=lambda o: hand_score(game, hands, o))


# -- proposals ---------------------------------------------------------------------------


def _jokers_on_sale(game) -> list[int]:
    return [i for i, c in enumerate(game.shop_jokers) if c.key.startswith("j_")]


def _buys(move: FactoredAction, index: int) -> bool:
    return int(move.action_type) in (BUY, BUY_AND_USE) and move.entity_target == index


def partners(key: str, held: set[str]) -> frozenset[str]:
    """The held jokers ``key`` pairs with in ``PAIRS``, either as their enabler or as one
    of the jokers an enabler boosts."""
    out: set[str] = set()
    for enabler, boosted in PAIRS:
        if key == enabler:
            out |= held & boosted
        elif key in boosted and enabler in held:
            out.add(enabler)
    return frozenset(out)


def linked(key: str, held: set[str], table) -> frozenset[str]:
    """What in ``held`` (joker and voucher keys) ``key`` is tied to in a voucher table
    (``VOUCHER_PAIRS`` or ``VOUCHER_CONFLICTS``): a voucher's jokers or a joker's vouchers."""
    out: set[str] = set()
    for vouchers, jokers in table:
        if key in vouchers:
            out |= held & jokers
        elif key in jokers:
            out |= held & vouchers
    return frozenset(out)


def _listed(joker: str, table) -> bool:
    """Whether ``joker`` is in a voucher table; owned vouchers (a game copy) are read only then."""
    return any(joker in jokers for _, jokers in table)


def owned_vouchers(game) -> frozenset[str]:
    return frozenset(game.to_dict()["sets"]["used_vouchers"])


def _redeems(move: FactoredAction, index: int) -> bool:
    return int(move.action_type) == REDEEM and move.entity_target == index


def propose(kind: str, info: dict, greedy: FactoredAction, rng: np.random.Generator,
            line: dict, gate) -> Proposal | None:
    """The ``kind`` branch for a main line at its decision (``info`` from its last step),
    or None when it does not come up there. ``gate()`` is asked once the branch has come
    up and before any lineup is scored; it marks the occasion and rolls the branch's
    chance, and the branch is tried only when it says so."""
    game, state, mask = info["game"], info["state"], info["action_mask"]
    if kind == "held tarot":
        found = _held_tarot(game, mask, greedy, line)
        return found if found is not None and gate() else None
    if kind in ("red seal", "upgraded card"):
        if game.phase.value != "pack_opening" or int(greedy.action_type) != PICK:
            return None
        legal = mask.entity_masks.get(ActionType.PickPackCard)
        cards = [c if legal is not None and legal[i] else None
                 for i, c in enumerate(game.pack_cards)]
        i = upgraded_pick(kind, cards, greedy.entity_target, rng)
        return Proposal(kind, [FactoredAction(PICK, entity_target=i)]) if i is not None and gate() else None
    if game.phase.value != "shop":
        return None
    owned = cache(lambda: owned_vouchers(game))
    if kind == "voucher synergy":
        held = {j.key for j in game.jokers}
        for i, v in enumerate(game.shop_vouchers):
            if _redeems(greedy, i) or not linked(v.key, held, VOUCHER_PAIRS):
                continue
            plan = redeem_plan(game, mask, i)
            if plan is not None:
                return Proposal(kind, plan) if gate() else None
        return None
    if kind in ("copy joker", "midas", "synergy"):
        held = {j.key for j in game.jokers}
        for i in _jokers_on_sale(game):
            key = game.shop_jokers[i].key
            if _buys(greedy, i):
                continue
            if kind == "copy joker":
                keep = frozenset() if key in COPY_JOKERS else None
            elif kind == "midas":
                keep = frozenset({VAMPIRE}) if key == MIDAS and VAMPIRE in held else None
            elif key in held:
                keep = None
            else:
                keep = partners(key, held)
                if not keep and not (_listed(key, VOUCHER_PAIRS) and linked(key, owned(), VOUCHER_PAIRS)):
                    keep = None
            if keep is None:
                continue
            plans = buy_plans(game, state, mask, i, keep)
            if plans:
                if not gate():
                    return None
                hands = dealt_hands(game, state, rng)
                plan = best_plan(game, plans, hands)
                if kind == "copy joker":
                    plan = placed(game, plan, hands, game.shop_jokers[i].sort_id)
                return Proposal(kind, plan)
        return None
    if kind == "off-build planet":
        if int(greedy.action_type) not in (BUY, BUY_AND_USE):
            return None
        card = game.shop_jokers[greedy.entity_target]
        hand = PLANET_HAND.get(card.key)
        played = {n: int(h.played) for n, h in game.hand_levels.items()}
        total = sum(played.values())
        if hand is None or total < MIN_PLAYS or played.get(hand, 0) / total >= OFF_BUILD:
            return None
        return Proposal(kind, [], veto=card.sort_id) if gate() else None
    if kind in ("pass joker", "conflict"):
        held = {j.key for j in game.jokers}
        if kind == "conflict" and int(greedy.action_type) == REDEEM:
            card = game.shop_vouchers[greedy.entity_target]
            vetoed = bool(linked(card.key, held, VOUCHER_CONFLICTS))
        elif int(greedy.action_type) != BUY:
            return None
        elif kind == "pass joker":
            card = game.shop_jokers[greedy.entity_target]
            vetoed = card.key in PASSED_OVER
        else:
            card = game.shop_jokers[greedy.entity_target]
            vetoed = bool(CONFLICTS.get(card.key, frozenset()) & held) or (
                _listed(card.key, VOUCHER_CONFLICTS) and bool(linked(card.key, owned(), VOUCHER_CONFLICTS)))
        if not vetoed:
            return None
        return Proposal(kind, [], veto=card.sort_id) if gate() else None
    if kind == "fill slot":
        if (int(greedy.action_type) != LEAVE or len(game.jokers) >= int(state.joker_slots)
                or MADNESS not in {j.key for j in game.jokers}):
            return None
        legal = mask.entity_masks.get(ActionType.BuyCard)
        options = [i for i in _jokers_on_sale(game) if legal is not None and legal[i]]
        if not options or not gate():
            return None
        plans = [[FactoredAction(BUY, entity_target=i)] for i in options]
        return Proposal(kind, best_plan(game, plans, dealt_hands(game, state, rng)))
    if kind == "best joker":
        t = int(greedy.action_type)
        if not (t == LEAVE or (t == BUY and game.shop_jokers[greedy.entity_target].key.startswith("j_"))):
            return None
        plans = [p for i in _jokers_on_sale(game) if not _buys(greedy, i)
                 for p in buy_plans(game, state, mask, i)]
        if not plans or not gate():
            return None
        hands = dealt_hands(game, state, rng)
        base = hand_score(game, hands)
        scored = [(hand_score(game, hands, p), p) for p in plans]
        score, plan = max(scored, key=lambda sp: sp[0])
        if score < base * (1 + GAIN) or score <= 0:
            return None
        return Proposal(kind, plan)
    raise ValueError(kind)


def note_held(game, line: dict) -> None:
    """Remember the shop visit count when each held consumable was first seen."""
    held = line.setdefault("held", {})
    for c in game.consumables:
        held.setdefault(c.sort_id, line["shops"])


def _held_tarot(game, mask, greedy: FactoredAction, line: dict) -> Proposal | None:
    """An enhancing tarot held through a whole shop visit, usable now and not the line's
    own pick: use it (its cards are the policy's likeliest, filled in by the caller)."""
    held = line.get("held", {})
    if game.phase.value != "selecting_hand":
        return None
    usable = mask.entity_masks.get(ActionType.UseConsumable)
    for i, c in enumerate(game.consumables):
        if (c.key in ENHANCING_TAROTS and held.get(c.sort_id, line["shops"]) < line["shops"]
                and usable is not None and usable[i]
                and not (int(greedy.action_type) == USE and greedy.entity_target == i)):
            return Proposal("held tarot", [FactoredAction(USE, entity_target=i)])
    return None


def upgraded(card) -> bool:
    """A playing card with an enhancement, a seal or an edition."""
    return card.key != "c_base" or bool(card.seal) or bool(card.edition)


def upgraded_pick(kind: str, cards: list, picked: int, rng: np.random.Generator) -> int | None:
    """The pack card a ``kind`` branch takes in place of the line's pick ``picked``
    (``cards`` holds None where the pick is not legal), or None when it does not come up:
    for "red seal", a Red Seal card when the pick is not one; for "upgraded card", when the
    pick is a plain card, a sealed card if there is one, else one with an enhancement or an
    edition. Ties are drawn at random."""
    pick = cards[picked] if picked is not None and picked < len(cards) else None
    if pick is None or not pick.front:
        return None
    shown = [(i, c) for i, c in enumerate(cards) if c is not None and c.front and not c.face_down]
    if kind == "red seal":
        pool = [] if pick.seal == RED_SEAL else [i for i, c in shown if c.seal == RED_SEAL]
    else:
        pool = [] if upgraded(pick) else (
            [i for i, c in shown if c.seal] or [i for i, c in shown if upgraded(c)])
    return int(rng.choice(pool)) if pool else None


def occasion(kind: str, line: dict) -> tuple:
    """What a branch of ``kind`` is tried once in: a shop visit, for a held tarot a blind,
    for a playing card pick the pack's cards as offered at that pick."""
    if kind in ("red seal", "upgraded card"):
        cards = line["info"]["game"].pack_cards
        return (kind, line["shops"], line["blinds"], tuple(c.sort_id for c in cards))
    return (kind, line["blinds"] if kind == "held tarot" else line["shops"])


def without(view, game, sort_id: int):
    """``view`` with the shop card or voucher ``sort_id`` out of the buys while it is on sale."""
    if game.phase.value != "shop":
        return view
    for area, types in ((game.shop_jokers, (ActionType.BuyCard, ActionType.BuyAndUse)),
                        (game.shop_vouchers, (ActionType.RedeemVoucher,))):
        at = [i for i, c in enumerate(area) if c.sort_id == sort_id]
        if at:
            break
    else:
        return view
    masks = dict(view.mask.entity_masks)
    type_mask = view.mask.type_mask.copy()
    for t in types:
        m = masks.get(t)
        if m is not None:
            m = m.copy()
            m[at[0]] = False
            masks[t] = m
            if not m.any():
                type_mask[int(t)] = False
    return replace(view, mask=replace(view.mask, entity_masks=masks, type_mask=type_mask))
