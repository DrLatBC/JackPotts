"""The policy network and how examples become its input.

Every hand card, joker, consumable, shop item and pack card is one token: its
observation row, plus learned embeddings of which item it is (jokers, consumables,
shop items, pack cards) or of its rank, suit, enhancement, edition and seal (playing
cards). One more token carries the 235 run-level numbers. A small transformer reads
the tokens. Three heads then pick an action the way Jackdaw's factored interface
takes it:

- the action type, from the run token;
- the item it points at (which shop card, which joker...), as a pointer over that
  item list's tokens;
- the hand cards it selects, by scoring every legal set of cards as a whole.

``Prepared`` holds a set of examples in the layout the network batches from,
``choose`` picks the most likely legal action for each example, and ``weigh`` shows
what the model weighed at one decision point.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from itertools import combinations

import numpy as np
import torch
from torch import nn

from jackdaw.env.action_space import NUM_ACTION_TYPES, ActionType
from jackdaw.env.game_spec import FactoredAction
from jackdaw.env.observation import (
    D_CONSUMABLE,
    D_GLOBAL,
    D_JOKER,
    D_PLAYING_CARD,
    D_SHOP,
    NUM_CENTER_KEYS,
)

from rl.dataset import Examples, bits_to_cards
from rl.features import (
    AURA_ID,
    CONSUMABLE,
    D_ROUND,
    ENTITY_LIST,
    ENTITY_SLOT,
    ENTITY_TYPES,
    HAND,
    HAND_EDITION,
    PACK,
    SHOP,
)

MAX_SELECT = 5
VALUE_SCALE = 8.0
"""The value head predicts points / VALUE_SCALE, so its targets stay near 0 to 2."""
BEST_HAND = 13
"""The column of a hand card's row that marks it as part of the best hand."""
D_RUN = D_GLOBAL + D_ROUND
"""The run token's input: the observation's global row, then the round columns."""
PLAY, DISCARD = int(ActionType.PlayHand), int(ActionType.Discard)
USE, PICK = int(ActionType.UseConsumable), int(ActionType.PickPackCard)

# Where each list's numbers sit in a token's feature vector: its observation row,
# then three more numbers for consumables and pack cards (needs targets, min and max
# targets) and for shop items (card, voucher or booster).
_WIDTHS = (D_PLAYING_CARD, D_JOKER, D_CONSUMABLE + 3, D_SHOP + 3, D_PLAYING_CARD + 3)
_AT = np.concatenate([[0], np.cumsum(_WIDTHS)])
D_TOKEN = int(_AT[-1])
_SEAL_COLUMN = 5
"""The column of a playing card's observation row that holds its seal."""
_LISTS = ("hand", "joker", "cons", "shop", "pack")

# Per action type: the list its item is in (-1: it points at nothing) and which
# part of the shop rows it counts from.
_TYPE_LIST = np.full(NUM_ACTION_TYPES, -1, np.int64)
_TYPE_PART = np.zeros(NUM_ACTION_TYPES, np.int64)
_TYPE_SLOT = np.zeros(NUM_ACTION_TYPES, np.int64)
for _t, (_list, _part) in ENTITY_LIST.items():
    _TYPE_LIST[_t], _TYPE_PART[_t], _TYPE_SLOT[_t] = _list, _part, ENTITY_SLOT[_t]


class Subsets:
    """Every selection of 1 to 5 cards from a hand of ``n``, in one fixed order."""

    _cache: dict[int, "Subsets"] = {}

    def __init__(self, n: int) -> None:
        sets = [c for size in range(1, MAX_SELECT + 1) for c in combinations(range(n), size)]
        self.member = np.zeros((len(sets), n), np.float32)
        self.bits = np.zeros(len(sets), np.int64)
        self.size = np.zeros(len(sets), np.int64)
        for row, cards in enumerate(sets):
            self.member[row, list(cards)] = 1.0
            self.bits[row] = sum(1 << c for c in cards)
            self.size[row] = len(cards)
        self.index = {int(b): row for row, b in enumerate(self.bits)}
        self.member_t = torch.from_numpy(self.member)
        self.size_t = torch.from_numpy(self.size)

    @classmethod
    def of(cls, n: int) -> "Subsets":
        if n not in cls._cache:
            cls._cache[n] = cls(n)
        return cls._cache[n]

    def legal(self, low, high, selectable, forced) -> np.ndarray:
        """(examples, subsets): which selections each example allows, given its bounds,
        its selectable cards (as bits) and its forced card (-1 for none)."""
        size, bits = self.size[None, :], self.bits[None, :]
        ok = (size >= low[:, None]) & (size <= high[:, None])
        ok &= (bits & ~selectable[:, None]) == 0
        need = np.where(forced >= 0, np.int64(1) << np.maximum(forced, 0), 0)
        ok &= (bits & need[:, None]) == need[:, None]
        return ok


def _bits(flags: np.ndarray, example: np.ndarray, within: np.ndarray, n: int) -> np.ndarray:
    """Per example, the flagged positions as a bit set."""
    weights = np.where(flags, np.exp2(within.astype(np.float64)), 0.0)
    return np.bincount(example, weights=weights, minlength=n).astype(np.int64)


class Prepared:
    """Examples laid out for batching: one row per token, and per example where its
    tokens and masks start."""

    def __init__(self, ex: Examples) -> None:
        a = ex.arrays
        self.ex = ex
        n = self.n = len(ex)
        counts = np.stack([a[name + "_n"] for name in _LISTS], 1).astype(np.int64)
        self.counts = counts
        self.list_at = np.concatenate([np.zeros((n, 1), np.int64), np.cumsum(counts, 1)[:, :-1]], 1)
        self.tok_len = counts.sum(1)
        self.tok_start = np.concatenate([[0], np.cumsum(self.tok_len)])
        total = int(self.tok_start[-1])
        self.feat = np.zeros((total + 1, D_TOKEN), np.float32)  # the last row is padding
        # Per token: list (1-5), item id, rank, suit, enhancement, edition, seal.
        self.cats = np.zeros((total + 1, 7), np.int16)
        splits = a["splits"].astype(np.int64)
        for li, name in enumerate(_LISTS):
            rows = a[name]
            cnt = counts[:, li]
            example = np.repeat(np.arange(n), cnt)
            within = np.arange(len(rows)) - np.repeat(ex.start[name][:-1], cnt)
            at = self.tok_start[example] + self.list_at[example, li] + within
            lo = int(_AT[li])
            self.feat[at, lo:lo + rows.shape[1]] = rows
            self.cats[at, 0] = li + 1
            if li in (HAND, PACK):
                seen = rows[:, 7] == 0  # a face-down card shows nothing
                self.cats[at, 2] = np.rint(rows[:, 0] * 14)
                self.cats[at, 3] = np.where(seen, np.rint(rows[:, 1] * 3) + 1, 0)
                self.cats[at, 4] = np.rint(rows[:, 3] * 8)
                self.cats[at, 5] = np.rint(rows[:, 4] * 4)
                self.cats[at, 6] = np.rint(rows[:, _SEAL_COLUMN] * 4)
            else:
                self.cats[at, 1] = np.rint(rows[:, 0] * NUM_CENTER_KEYS)
            if li == HAND:
                self.card_bits = _bits(a["card_mask"], example, within, n)
                self.plain_bits = _bits(rows[:, HAND_EDITION] == 0, example, within, n)
                self.best_bits = _bits(rows[:, BEST_HAND] > 0.5, example, within, n)
            elif li == CONSUMABLE:
                extra = a["cons_extra"]
                self.feat[at, lo + D_CONSUMABLE:lo + D_CONSUMABLE + 3] = extra / (1, 5, 5)
            elif li == SHOP:
                s = splits[example]
                part = (within >= s[:, 0]).astype(np.int64) + (within >= s[:, 0] + s[:, 1])
                self.feat[at, lo + D_SHOP + part] = 1.0
            elif li == PACK:
                extra = a["pack_extra"]
                self.cats[at, 1] = extra[:, 0]
                self.feat[at, lo + D_PLAYING_CARD:lo + D_PLAYING_CARD + 3] = extra[:, 1:] / (1, 5, 5)
        self.part_at = np.concatenate(
            [np.zeros((n, 1), np.int64), splits[:, :1], splits[:, :1] + splits[:, 1:2]], 1
        )
        emask_len = a["emask_len"].astype(np.int64)
        self.emask_len = emask_len
        self.emask_at = ex.start["emask"][:-1, None] + np.concatenate(
            [np.zeros((n, 1), np.int64), np.cumsum(emask_len, 1)[:, :-1]], 1
        )
        self.emask = np.concatenate([a["emask"], [False]])
        self.n_hand = counts[:, HAND]
        self.act_type = a["act_type"].astype(np.int64)
        self.act_entity = a["act_entity"].astype(np.int64)
        self.act_cards = a["act_cards"].astype(np.int64)
        self.forced = a["forced"].astype(np.int64)
        self.zeroed, self.zeroed_at = a["zeroed"].astype(np.int64), ex.start["zeroed"]

    # -- per batch ---------------------------------------------------------------

    def without_seals(self) -> "Prepared":
        """The same examples with the hand and pack cards' seals hidden from the network."""
        out = copy.copy(self)
        out.feat, out.cats = self.feat.copy(), self.cats.copy()
        for li in (HAND, PACK):
            cards = out.cats[:, 0] == li + 1
            out.cats[cards, 6] = 0
            out.feat[cards, int(_AT[li]) + _SEAL_COLUMN] = 0.0
        return out

    def tokens(self, idx: np.ndarray) -> dict[str, torch.Tensor]:
        """The network's input for the examples ``idx``."""
        lens = self.tok_len[idx]
        width = max(int(lens.max()), 1)
        pos = np.arange(width)[None, :]
        real = pos < lens[:, None]
        at = np.where(real, self.tok_start[idx][:, None] + pos, len(self.feat) - 1)
        return {
            "glob": torch.from_numpy(np.concatenate([self.ex["glob"][idx], self.ex["round"][idx]], 1)),
            "feat": torch.from_numpy(self.feat[at]),
            "cats": torch.from_numpy(self.cats[at].astype(np.int64)),
            "pad": torch.from_numpy(~real),
            "type_mask": torch.from_numpy(self.ex["type_mask"][idx]),
        }

    def entity_base(self, idx: np.ndarray, types: np.ndarray) -> np.ndarray:
        """The token position of item 0 for each example's action type."""
        lists = np.maximum(_TYPE_LIST[types], 0)
        base = self.list_at[idx, lists]
        return base + np.where(lists == SHOP, self.part_at[idx, _TYPE_PART[types]], 0)

    def entity_mask(self, idx: np.ndarray, types: np.ndarray, width: int) -> np.ndarray:
        """(examples, tokens): the tokens each example's action type may point at."""
        slot = _TYPE_SLOT[types]
        points = _TYPE_LIST[types] >= 0
        lens = np.where(points, self.emask_len[idx, slot], 0)
        span = max(int(lens.max()), 1)
        pos = np.arange(span)[None, :]
        real = pos < lens[:, None]
        legal = self.emask[np.where(real, self.emask_at[idx, slot][:, None] + pos, -1)]
        token = self.entity_base(idx, types)[:, None] + pos
        rows = np.broadcast_to(np.arange(len(idx))[:, None], real.shape)
        out = np.zeros((len(idx), width), bool)
        out[rows[real], token[real]] = legal[real]
        return out

    def card_query(self, idx: np.ndarray, types: np.ndarray, entities: np.ndarray):
        """For each example's action: whether it selects cards, the bounds, and which
        cards it may select (as bits)."""
        a = self.ex.arrays
        n = len(idx)
        has = np.zeros(n, bool)
        low = np.zeros(n, np.int64)
        high = np.zeros(n, np.int64)
        selectable = self.card_bits[idx].copy()
        plays = (types == PLAY) | (types == DISCARD)
        has[plays] = True
        low[plays] = a["min_sel"][idx[plays]]
        high[plays] = a["max_sel"][idx[plays]]
        for action, name, list_id, col in ((USE, "cons_extra", CONSUMABLE, 0), (PICK, "pack_extra", PACK, 1)):
            rows = np.flatnonzero((types == action) & (entities >= 0))
            if not len(rows):
                continue
            ex_rows = idx[rows]
            extra = a[name][self.ex.start[name][ex_rows] + entities[rows]]
            has[rows] = extra[:, col] > 0.5
            low[rows] = extra[:, col + 1]
            high[rows] = extra[:, col + 2]
            token = self.tok_start[ex_rows] + self.list_at[ex_rows, list_id] + entities[rows]
            aura = self.cats[token, 1] == AURA_ID
            selectable[rows[aura]] &= self.plain_bits[ex_rows[aura]]
        return has, low, high, selectable


class Policy(nn.Module):
    def __init__(self, width: int = 128, layers: int = 2, heads: int = 4, dropout: float = 0.0):
        super().__init__()
        self.width = width
        self.run_in = nn.Sequential(nn.Linear(D_RUN, width), nn.ReLU(), nn.Linear(width, width))
        self.token_in = nn.Linear(D_TOKEN, width)
        sizes = (6, NUM_CENTER_KEYS + 1, 15, 5, 9, 5, 5)
        self.embed = nn.ModuleList(nn.Embedding(s, width, padding_idx=0) for s in sizes)
        layer = nn.TransformerEncoderLayer(
            width, heads, 2 * width, dropout, batch_first=True, norm_first=True
        )
        self.encoder = nn.TransformerEncoder(layer, layers, enable_nested_tensor=False)
        self.norm = nn.LayerNorm(width)
        self.type_head = nn.Linear(width, NUM_ACTION_TYPES)
        self.type_embed = nn.Embedding(NUM_ACTION_TYPES, width)
        self.pointer = nn.Sequential(nn.Linear(2 * width, width), nn.ReLU(), nn.Linear(width, width))
        self.card_in = nn.Linear(width, width)
        self.set_context = nn.Sequential(nn.Linear(3 * width, width), nn.ReLU(), nn.Linear(width, width))
        self.size_embed = nn.Embedding(MAX_SELECT + 1, width)
        self.set_score = nn.Sequential(
            nn.ReLU(), nn.Linear(width, width), nn.ReLU(), nn.Linear(width, 1)
        )
        self.value_head = nn.Sequential(nn.Linear(width, width), nn.ReLU(), nn.Linear(width, 1))

    def encode(self, t: dict[str, torch.Tensor]) -> tuple[torch.Tensor, torch.Tensor]:
        """The run token and the item tokens after the transformer."""
        x = self.token_in(t["feat"])
        for column, embed in enumerate(self.embed):
            x = x + embed(t["cats"][..., column])
        x = torch.cat([self.run_in(t["glob"])[:, None, :], x], 1)
        pad = torch.cat([torch.zeros_like(t["pad"][:, :1]), t["pad"]], 1)
        x = self.norm(self.encoder(x, src_key_padding_mask=pad))
        return x[:, 0], x[:, 1:]

    def value(self, run: torch.Tensor) -> torch.Tensor:
        """The points the rest of the game is expected to bring, divided by
        ``VALUE_SCALE``."""
        return self.value_head(run).squeeze(-1)

    def type_logits(self, run: torch.Tensor, type_mask: torch.Tensor) -> torch.Tensor:
        return self.type_head(run).masked_fill(~type_mask, -torch.inf)

    def entity_logits(self, run, tokens, types, mask) -> torch.Tensor:
        """A score for every token as the item the action points at."""
        query = self.pointer(torch.cat([run, self.type_embed(types)], -1))
        scores = torch.einsum("bld,bd->bl", tokens, query) / self.width ** 0.5
        return scores.masked_fill(~mask, -torch.inf)

    def set_logits(self, run, hand, types, item, subsets: Subsets, legal) -> torch.Tensor:
        """A score for every selection of the hand's cards. ``item`` is the token the
        action points at (zeros when it points at none)."""
        cards = self.card_in(hand)
        pooled = torch.einsum("sn,bnd->bsd", subsets.member_t, cards)
        context = self.set_context(torch.cat([run, self.type_embed(types), item], -1))
        x = pooled + context[:, None, :] + self.size_embed(subsets.size_t)[None, :, :]
        return self.set_score(x).squeeze(-1).masked_fill(~legal, -torch.inf)


def _pad_columns(t: torch.Tensor, width: int) -> torch.Tensor:
    return torch.cat([t, t.new_zeros(*t.shape[:-1], width - t.shape[-1])], -1)


def widen_inputs(state: dict) -> dict:
    """A saved network state whose run input is narrower than ``D_RUN``, with zero
    weights on the missing columns: the network acts exactly as saved until training
    moves them."""
    w = state.get("run_in.0.weight")
    if w is None or w.shape[1] >= D_RUN:
        return state
    return {**state, "run_in.0.weight": _pad_columns(w, D_RUN)}


def widen_optimizer(opt: torch.optim.Optimizer) -> None:
    """Optimizer moments loaded for a narrower run input, padded with zeros to the
    parameters' shapes."""
    for group in opt.param_groups:
        for p in group["params"]:
            for key, v in opt.state.get(p, {}).items():
                if torch.is_tensor(v) and v.dim() == p.dim() and v.shape != p.shape:
                    opt.state[p][key] = _pad_columns(v, p.shape[-1])


def set_groups(prep: Prepared, idx, rows, types, entities):
    """The card-selecting examples among ``rows`` (positions in the batch), grouped by
    hand size: ``(rows, subsets, legal)`` per group. A play's legal selections leave out
    the ones the boss would score 0 (``rl.features.zeroed_plays``)."""
    has, low, high, selectable = prep.card_query(idx[rows], types[rows], entities[rows])
    rows, low, high, selectable = rows[has], low[has], high[has], selectable[has]
    sizes = prep.n_hand[idx[rows]]
    for n in np.unique(sizes):
        pick = sizes == n
        subsets = Subsets.of(int(n))
        legal = subsets.legal(low[pick], high[pick], selectable[pick], prep.forced[idx[rows[pick]]])
        for k, (row, example) in enumerate(zip(rows[pick], idx[rows[pick]])):
            if types[row] == PLAY:
                zeroed = prep.zeroed[prep.zeroed_at[example]:prep.zeroed_at[example + 1]]
                if len(zeroed):
                    legal[k] &= ~np.isin(subsets.bits, zeroed)
        yield rows[pick], subsets, legal


def item_tokens(tokens: torch.Tensor, rows, position) -> torch.Tensor:
    """The token each of ``rows`` points at; zeros where ``position`` is negative."""
    position = torch.as_tensor(position)
    picked = tokens[torch.as_tensor(rows), position.clamp(min=0)]
    return picked * (position >= 0)[:, None]


def _pick(logits: torch.Tensor, generator: torch.Generator | None) -> tuple[np.ndarray, torch.Tensor]:
    """Per row, the top choice, or one drawn in proportion to its probability when a
    ``generator`` is given; with the chosen entry's log-probability."""
    if generator is None:
        chosen = logits.argmax(-1)
    else:
        chosen = torch.multinomial(torch.softmax(logits, -1), 1, generator=generator).squeeze(-1)
    return chosen.numpy(), _taken(logits, chosen)


def _taken(logits: torch.Tensor, chosen: torch.Tensor) -> torch.Tensor:
    return torch.log_softmax(logits, -1).gather(-1, chosen[:, None]).squeeze(-1)


@torch.no_grad()
def act(model: Policy, prep: Prepared, idx: np.ndarray | None = None,
        generator: torch.Generator | None = None):
    """An action for each example: the type first, then the item that type points at,
    then the cards, each the most likely legal choice or, with a ``generator``, drawn
    from the policy. Returns the actions, their log-probabilities and the value in
    points."""
    idx = np.arange(prep.n) if idx is None else idx
    t = prep.tokens(idx)
    run, tokens = model.encode(t)
    types, logp = _pick(model.type_logits(run, t["type_mask"]), generator)
    actions, logp = _complete(model, prep, idx, run, tokens, types, logp, generator)
    return actions, logp.numpy(), model.value(run).numpy() * VALUE_SCALE


def _complete(model: Policy, prep: Prepared, idx, run, tokens, types: np.ndarray,
              logp: torch.Tensor, generator: torch.Generator | None,
              fixed: list[FactoredAction] | None = None):
    """Each row's action of type ``types[row]``: its item, then its cards. ``logp`` comes
    in as the types' log-probabilities and goes out as the whole actions'. With ``fixed``,
    each row's item, and its cards where the fixed move gives them, are that move's."""
    entities = np.full(len(idx), -1, np.int64)
    position = np.full(len(idx), -1, np.int64)
    rows = np.flatnonzero(_TYPE_LIST[types] >= 0)
    if len(rows):
        mask = prep.entity_mask(idx[rows], types[rows], tokens.shape[1])
        if not mask.any(1).all():
            raise RuntimeError("an action type is legal but none of its items is")
        logits = model.entity_logits(
            run[rows], tokens[rows], torch.from_numpy(types[rows]), torch.from_numpy(mask)
        )
        base = prep.entity_base(idx[rows], types[rows])
        if fixed is None:
            position[rows], item_logp = _pick(logits, generator)
        else:
            position[rows] = base + np.array([fixed[r].entity_target for r in rows], np.int64)
            item_logp = _taken(logits, torch.from_numpy(position[rows]))
        logp[rows] += item_logp
        entities[rows] = position[rows] - base
    cards: dict[int, tuple[int, ...]] = {}
    every = np.arange(len(idx))
    for group, subsets, legal in set_groups(prep, idx, every, types, entities):
        if not legal.any(1).all():
            raise RuntimeError("an action selects cards but no selection is legal")
        n = subsets.member.shape[1]
        logits = model.set_logits(
            run[group], tokens[group, :n], torch.from_numpy(types[group]),
            item_tokens(tokens, group, position[group]), subsets, torch.from_numpy(legal),
        )
        chosen, set_logp = _pick(logits, generator)
        given = [k for k, row in enumerate(group)
                 if fixed is not None and fixed[row].card_target is not None]
        if given:
            bits = subsets.bits.tolist()
            for k in given:
                chosen[k] = bits.index(sum(1 << c for c in fixed[group[k]].card_target))
            set_logp = _taken(logits, torch.from_numpy(chosen))
        logp[group] += set_logp
        for row, best in zip(group, chosen):
            cards[int(row)] = bits_to_cards(int(subsets.bits[best]))
    actions = [
        FactoredAction(
            action_type=int(types[row]),
            card_target=cards.get(row),
            entity_target=None if entities[row] < 0 else int(entities[row]),
        )
        for row in range(len(idx))
    ]
    return actions, logp


@dataclass
class Weighed:
    """What the model weighed at one decision point."""

    value: float
    """The points the rest of the game is expected to bring."""
    types: dict[int, float]
    """Every legal action type's probability."""
    options: list[tuple[FactoredAction, float]]
    """The likeliest action of each of the likeliest types, with the probability of its
    item and cards given its type; the first is ``choose``'s pick."""


@torch.no_grad()
def weigh(model: Policy, prep: Prepared, i: int = 0, top: int = 3) -> Weighed:
    """Example ``i`` as the model weighs it, with the options of its ``top`` likeliest
    action types."""
    t = prep.tokens(np.array([i]))
    run, tokens = model.encode(t)
    type_logp = torch.log_softmax(model.type_logits(run, t["type_mask"]), -1)[0]
    probs = type_logp.exp()
    order = torch.argsort(probs, descending=True, stable=True)
    order = order[probs[order] > 0][:top].numpy()
    k = len(order)
    actions, logp = _complete(model, prep, np.full(k, i), run.expand(k, -1),
                              tokens.expand(k, -1, -1), order, torch.zeros(k), None)
    return Weighed(
        value=float(model.value(run)[0]) * VALUE_SCALE,
        types={int(ty): float(probs[ty]) for ty in torch.nonzero(probs > 0).flatten()},
        options=[(a, float(p)) for a, p in zip(actions, logp.exp())],
    )


@torch.no_grad()
def complete(model: Policy, prep: Prepared, idx: np.ndarray,
             moves: list[FactoredAction]) -> tuple[list[FactoredAction], np.ndarray]:
    """Per example ``idx[row]``, the move ``moves[row]`` with its cards filled in where it
    gives none and needs some (the policy's likeliest), and the whole move's
    log-probability."""
    t = prep.tokens(idx)
    run, tokens = model.encode(t)
    types = np.array([m.action_type for m in moves], np.int64)
    logp = _taken(model.type_logits(run, t["type_mask"]), torch.from_numpy(types))
    actions, logp = _complete(model, prep, idx, run, tokens, types, logp, None, fixed=moves)
    return actions, logp.numpy()


def choose(model: Policy, prep: Prepared, idx: np.ndarray | None = None) -> list[FactoredAction]:
    """The most likely legal action for each example."""
    return act(model, prep, idx)[0]
