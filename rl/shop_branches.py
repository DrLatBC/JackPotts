"""Shop branches: buys and uses a copy of the bot under-tries, each tried on a branch so
the result teaches the policy what it is worth (``rl.branch``).

A proposal is the moves the branch makes in place of the main line's own (a sale or two,
then the buy), or, for a planet bought for a hand the run does not play, the card the
branch may not buy this shop visit. Jokers are judged by what Tyler asked of them: does
the trade raise the average score of the best play over hands dealt from the run's deck
(``hand_score``)? The test only decides whether a trade is tried; the branch's result is
what the policy learns from. Scaling and money jokers score little up front, so the test
passes them over, as the policy does now.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import combinations

import numpy as np

from jackdaw.core.game import IllegalActionError
from jackdaw.engine import actions as A
from jackdaw.engine.data import load
from jackdaw.env.action_space import ActionType
from jackdaw.env.game_spec import FactoredAction

BUY, BUY_AND_USE = int(ActionType.BuyCard), int(ActionType.BuyAndUse)
SELL_JOKER, SELL_CONS = int(ActionType.SellJoker), int(ActionType.SellConsumable)
USE, LEAVE = int(ActionType.UseConsumable), int(ActionType.NextRound)

_CENTERS = load("centers")
ENHANCING_TAROTS = frozenset(
    k for k, c in _CENTERS.items()
    if c.get("set") == "Tarot" and str((c.get("config") or {}).get("mod_conv", "")).startswith("m_"))
"""Tarots that enhance the cards they target (Magician, Empress, Lovers, Devil, ...)."""
PLANET_HAND = {k: c["config"]["hand_type"] for k, c in _CENTERS.items()
               if c.get("set") == "Planet" and "hand_type" in (c.get("config") or {})}
COPY_JOKERS = frozenset({"j_blueprint", "j_brainstorm"})
MIDAS, VAMPIRE = "j_midas_mask", "j_vampire"

KINDS = ("copy joker", "midas", "best joker", "fill slot", "off-build planet", "held tarot")
"""Tried in this order; a main line takes at most one branch per tick."""
SHOP_CHANCE: dict[str, float] = {
    "copy joker": 1.0, "midas": 1.0, "best joker": 0.25, "fill slot": 0.5,
    "off-build planet": 0.5, "held tarot": 1.0,
}
"""The chance each branch is tried when it comes up: once per shop visit, a held tarot
once per blind."""
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
    """``n`` hands of the run's hand size, as positions in its deck sorted by sort id."""
    size = len(game.deck) + len(game.hand) + len(game.discard_pile)
    k = min(int(state.hand_size), size)
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
    raise ValueError(f"not a shop move: {m}")


# -- buying, with sales if needed --------------------------------------------------------


def buy_plans(game, state, mask, shop_index: int) -> list[list[FactoredAction]]:
    """The ways to buy shop card ``shop_index``: as is, or after selling at most one
    joker and then consumables (from the last) until it is affordable. Each is checked on a
    copy of the game, so only ways the game takes are given; empty when none is."""
    card = game.shop_jokers[shop_index]
    sell_j = mask.entity_masks.get(ActionType.SellJoker)
    sell_c = mask.entity_masks.get(ActionType.SellConsumable)
    jokers = [i for i in range(len(game.jokers)) if sell_j is not None and sell_j[i]]
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


# -- proposals ---------------------------------------------------------------------------


def _jokers_on_sale(game) -> list[int]:
    return [i for i, c in enumerate(game.shop_jokers) if c.key.startswith("j_")]


def _buys(move: FactoredAction, index: int) -> bool:
    return int(move.action_type) in (BUY, BUY_AND_USE) and move.entity_target == index


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
    if game.phase.value != "shop":
        return None
    if kind in ("copy joker", "midas"):
        wanted = COPY_JOKERS if kind == "copy joker" else (
            {MIDAS} if any(j.key == VAMPIRE for j in game.jokers) else set())
        for i in _jokers_on_sale(game):
            if game.shop_jokers[i].key in wanted and not _buys(greedy, i):
                plans = buy_plans(game, state, mask, i)
                if plans:
                    if not gate():
                        return None
                    return Proposal(kind, best_plan(game, plans, dealt_hands(game, state, rng)))
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
    if kind == "fill slot":
        if int(greedy.action_type) != LEAVE or len(game.jokers) >= int(state.joker_slots):
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


def occasion(kind: str, line: dict) -> tuple:
    """What a branch of ``kind`` is tried once in: a shop visit, or for a held tarot a blind."""
    return (kind, line["blinds"] if kind == "held tarot" else line["shops"])


def without(view, game, sort_id: int):
    """``view`` with the shop card ``sort_id`` out of the buys while it is on sale."""
    at = [i for i, c in enumerate(game.shop_jokers) if c.sort_id == sort_id]
    if not at or game.phase.value != "shop":
        return view
    masks = dict(view.mask.entity_masks)
    type_mask = view.mask.type_mask.copy()
    for t in (ActionType.BuyCard, ActionType.BuyAndUse):
        m = masks.get(t)
        if m is not None:
            m = m.copy()
            m[at[0]] = False
            masks[t] = m
            if not m.any():
                type_mask[int(t)] = False
    return replace(view, mask=replace(view.mask, entity_masks=masks, type_mask=type_mask))
