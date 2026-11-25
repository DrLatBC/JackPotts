"""What the model sees at one decision: Jackdaw's observation and legal-action mask.

``view_of`` reads both from a ``jackdaw.core.Game`` the same way
``jackdaw.env.BalatroEnvironment`` does (one ``env_view`` call under "vanilla"
legality), and adds what the observation leaves out for an opened pack. The
converter calls it on a replayed game and the evaluator on the environment's
game, so training and play see the same numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import numpy as np

from jackdaw.engine.data import load
from jackdaw.env.action_space import (
    DEFAULT_MAX_FREE_MOVES,
    ActionMask,
    ActionType,
    get_consumable_target_info,
)
from jackdaw.env.balatro_env import _rust_view
from jackdaw.env.game_interface import GameState
from jackdaw.env.observation import NUM_CENTER_KEYS, Observation, center_key_id

LEGALITY = "vanilla"
MAX_FREE_MOVES = DEFAULT_MAX_FREE_MOVES

D_PACK_EXTRA = 4
"""Per pack card: item id, needs targets, min targets, max targets."""
D_CONS_EXTRA = 3
"""Per consumable: needs targets, min targets, max targets, as the engine judges a use
(the observation's own minimum is not the engine's)."""

BOSS_KEYS: tuple[str, ...] = tuple(sorted(k for k in load("blinds") if k not in ("bl_small", "bl_big")))
HAND_TYPES: tuple[str, ...] = tuple(sorted(load("hands")))
_BOSS_AT = {k: i for i, k in enumerate(BOSS_KEYS)}
_HAND_AT = {h: len(BOSS_KEYS) + i for i, h in enumerate(HAND_TYPES)}
D_ROUND = len(BOSS_KEYS) + len(HAND_TYPES)
"""Per decision, what the observation leaves out of a round: the boss whose effect is in
force (one column per ``BOSS_KEYS``), then each of ``HAND_TYPES`` played this round (1 or
0). All zero outside a blind."""

# Action types that point at one item, with the list they index.
ENTITY_TYPES: tuple[int, ...] = (
    ActionType.BuyCard, ActionType.SellJoker, ActionType.SellConsumable,
    ActionType.UseConsumable, ActionType.RedeemVoucher, ActionType.OpenBooster,
    ActionType.PickPackCard, ActionType.SwapJokersLeft, ActionType.SwapJokersRight,
    ActionType.SwapHandLeft, ActionType.SwapHandRight, ActionType.BuyAndUse,
)
ENTITY_SLOT: dict[int, int] = {int(t): i for i, t in enumerate(ENTITY_TYPES)}

# The observation's lists, in token order.
HAND, JOKER, CONSUMABLE, SHOP, PACK = range(5)

AURA_ID = center_key_id("c_aura")
HAND_EDITION = 4
"""The edition column of a hand card's row."""

# (list, which part of the shop rows) each entity action indexes. The shop rows are
# cards, then vouchers, then boosters; each buy action counts from its own part.
ENTITY_LIST: dict[int, tuple[int, int]] = {
    int(ActionType.BuyCard): (SHOP, 0),
    int(ActionType.BuyAndUse): (SHOP, 0),
    int(ActionType.RedeemVoucher): (SHOP, 1),
    int(ActionType.OpenBooster): (SHOP, 2),
    int(ActionType.SellJoker): (JOKER, 0),
    int(ActionType.SwapJokersLeft): (JOKER, 0),
    int(ActionType.SwapJokersRight): (JOKER, 0),
    int(ActionType.SellConsumable): (CONSUMABLE, 0),
    int(ActionType.UseConsumable): (CONSUMABLE, 0),
    int(ActionType.PickPackCard): (PACK, 0),
    int(ActionType.SwapHandLeft): (HAND, 0),
    int(ActionType.SwapHandRight): (HAND, 0),
}


@dataclass
class View:
    """One decision point as the model sees it."""

    obs: Observation
    mask: ActionMask
    state: GameState
    splits: tuple[int, int, int]
    """How many shop rows are cards, vouchers and boosters."""
    pack_extra: np.ndarray
    """(n_pack, D_PACK_EXTRA): what the observation's blank pack rows leave out."""
    cons_extra: np.ndarray
    """(n_consumables, D_CONS_EXTRA): each consumable's targeting needs."""
    round_extra: np.ndarray
    """(D_ROUND,): the boss in force and the hand types played this round."""
    zeroed: tuple[int, ...] = ()
    """The plays (card selections as bits) the boss would score 0 while some other play
    scores: ``ZEROING_BOSSES`` only. A model never makes them."""

    def card_bounds(self, action_type: int, entity: int | None) -> tuple[int, int] | None:
        """How many hand cards the action selects: ``(min, max)``, or ``None`` when it
        selects none. Against Cerulean Bell the bounds count the forced card."""
        if action_type in (ActionType.PlayHand, ActionType.Discard):
            return self.mask.min_card_select, self.mask.max_card_select
        if action_type == ActionType.UseConsumable and entity is not None:
            needs, low, high = self.cons_extra[entity]
            return (int(low), int(high)) if needs > 0.5 else None
        if action_type == ActionType.PickPackCard and entity is not None:
            _, needs, low, high = self.pack_extra[entity]
            return (int(low), int(high)) if needs > 0.5 else None
        return None

    def selectable(self, action_type: int, entity: int | None) -> np.ndarray:
        """Which hand cards the action may select. Aura only takes a card without an
        edition; everything else takes any card the mask offers."""
        cards = self.mask.card_mask
        if entity is None:
            return cards
        if action_type == ActionType.UseConsumable:
            item = item_ids(self.obs.consumables[entity, :1])[0]
        elif action_type == ActionType.PickPackCard:
            item = int(self.pack_extra[entity, 0])
        else:
            return cards
        if item == AURA_ID:
            return cards & (self.obs.hand_cards[:, HAND_EDITION] == 0)
        return cards

    def legal_card_sets(self, action_type: int, entity: int | None) -> list[tuple[int, ...]]:
        """Every card selection the mask allows for the action, as sorted index tuples."""
        bounds = self.card_bounds(action_type, entity)
        if bounds is None:
            return []
        return card_sets(
            self.selectable(action_type, entity), bounds[0], bounds[1], self.mask.forced_card
        )


def card_sets(
    card_mask: np.ndarray, low: int, high: int, forced: int | None
) -> list[tuple[int, ...]]:
    """The selections of ``low`` to ``high`` selectable hand cards that include ``forced``."""
    if forced is not None and not card_mask[forced]:
        return []
    free = [int(i) for i in np.flatnonzero(card_mask) if i != forced]
    base = () if forced is None else (forced,)
    out: list[tuple[int, ...]] = []
    for size in range(max(low, 1), high + 1):
        for rest in combinations(free, size - len(base)):
            out.append(tuple(sorted(base + rest)))
    return out


def view_of(game, free: bool = True) -> View:
    """The decision point a run is at. ``free`` is whether reordering moves are still
    offered (the environment stops offering them after ``MAX_FREE_MOVES`` in a row)."""
    return view_from(game, *_rust_view(game._g.env_view(LEGALITY, free)))


def view_from(game, obs: Observation, mask: ActionMask, state: GameState, splits) -> View:
    """The view for an observation and mask already read from ``game`` (the
    environment returns both with every step)."""
    n_pack = obs.pack_cards.shape[0]
    extra = np.zeros((n_pack, D_PACK_EXTRA), np.float32)
    if n_pack:
        for i, card in enumerate(game.pack_cards):
            item = center_key_id(card.key)
            extra[i] = (item, *_targets(game, "pack_cards", i, item))
    n_cons = obs.consumables.shape[0]
    cons = np.zeros((n_cons, D_CONS_EXTRA), np.float32)
    for i, item in enumerate(item_ids(obs.consumables[:, 0])):
        cons[i] = _targets(game, "consumables", i, item)
    return View(obs, mask, state, tuple(splits), extra, cons, round_facts(game),
                zeroed_plays(game, mask))


ZEROING_BOSSES = frozenset({"bl_eye", "bl_mouth"})
"""Bosses that score a play 0 by its hand type: The Eye (a type already played this
round), The Mouth (any type but the round's first)."""


def zeroed_plays(game, mask: ActionMask) -> tuple[int, ...]:
    """The legal plays the boss in force scores 0, as card bits, when at least one legal
    play still scores; otherwise none. The engine's own preview judges each play, so a
    disabled boss or a joker that changes hand types is accounted for."""
    if not mask.type_mask[ActionType.PlayHand]:
        return ()
    blind = game._view("blind")
    if not blind or blind.get("key") not in ZEROING_BOSSES or blind.get("disabled"):
        return ()
    if not any(h.get("played_this_round", 0) for h in game._view("hands").values()):
        return ()
    forced = mask.forced_card
    sets = card_sets(mask.card_mask, mask.min_card_select, mask.max_card_select, forced)
    zeroed = tuple(sum(1 << c for c in cards) for cards in sets
                   if game.preview_play(list(cards)).debuffed)
    return zeroed if len(zeroed) < len(sets) else ()


def round_facts(game) -> np.ndarray:
    """The ``D_ROUND`` columns for the blind being played. Hands' this-round counts
    stay set through the shop, so they are read only while a blind is in play."""
    out = np.zeros(D_ROUND, np.float32)
    blind = game._view("blind")
    if not blind or not blind.get("key"):
        return out
    if blind.get("boss") and not blind.get("disabled") and blind["key"] in _BOSS_AT:
        out[_BOSS_AT[blind["key"]]] = 1.0
    for hand, level in game._view("hands").items():
        if level.get("played_this_round", 0) > 0 and hand in _HAND_AT:
            out[_HAND_AT[hand]] = 1.0
    return out


def _targets(game, area: str, index: int, item: int) -> tuple[float, int, int]:
    """``(needs targets, min, max)`` for a consumable. The engine's target info says
    Aura needs none, yet a use of it must select exactly one card."""
    if item == AURA_ID:
        return 1.0, 1, 1
    low, high, needs = get_consumable_target_info(game, area, index)
    return float(needs), low, high


def item_ids(column: np.ndarray) -> np.ndarray:
    """Item ids from an observation's item-id column (0 is unknown)."""
    return np.rint(column * NUM_CENTER_KEYS).astype(np.int64)
