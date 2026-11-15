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

``Prepared`` holds a set of examples in the layout the network batches from, and
``choose`` picks the most likely legal action for each example.
"""

from __future__ import annotations

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
    ENTITY_LIST,
    ENTITY_SLOT,
    ENTITY_TYPES,
    HAND,
    HAND_EDITION,
    PACK,
    SHOP,
)

MAX_SELECT = 5
BEST_HAND = 13
"""The column of a hand card's row that marks it as part of the best hand."""
PLAY, DISCARD = int(ActionType.PlayHand), int(ActionType.Discard)
USE, PICK = int(ActionType.UseConsumable), int(ActionType.PickPackCard)

# Where each list's numbers sit in a token's feature vector: its observation row,
# then three more numbers for consumables and pack cards (needs targets, min and max
# targets) and for shop items (card, voucher or booster).
_WIDTHS = (D_PLAYING_CARD, D_JOKER, D_CONSUMABLE + 3, D_SHOP + 3, D_PLAYING_CARD + 3)
_AT = np.concatenate([[0], np.cumsum(_WIDTHS)])
D_TOKEN = int(_AT[-1])
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
                self.cats[at, 6] = np.rint(rows[:, 5] * 4)
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

    # -- per batch ---------------------------------------------------------------

    def tokens(self, idx: np.ndarray) -> dict[str, torch.Tensor]:
        """The network's input for the examples ``idx``."""
        lens = self.tok_len[idx]
        width = max(int(lens.max()), 1)
        pos = np.arange(width)[None, :]
        real = pos < lens[:, None]
        at = np.where(real, self.tok_start[idx][:, None] + pos, len(self.feat) - 1)
        return {
            "glob": torch.from_numpy(self.ex["glob"][idx]),
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
        self.run_in = nn.Sequential(nn.Linear(D_GLOBAL, width), nn.ReLU(), nn.Linear(width, width))
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

    def encode(self, t: dict[str, torch.Tensor]) -> tuple[torch.Tensor, torch.Tensor]:
        """The run token and the item tokens after the transformer."""
        x = self.token_in(t["feat"])
        for column, embed in enumerate(self.embed):
            x = x + embed(t["cats"][..., column])
        x = torch.cat([self.run_in(t["glob"])[:, None, :], x], 1)
        pad = torch.cat([torch.zeros_like(t["pad"][:, :1]), t["pad"]], 1)
        x = self.norm(self.encoder(x, src_key_padding_mask=pad))
        return x[:, 0], x[:, 1:]

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


def set_groups(prep: Prepared, idx, rows, types, entities):
    """The card-selecting examples among ``rows`` (positions in the batch), grouped by
    hand size: ``(rows, subsets, legal)`` per group."""
    has, low, high, selectable = prep.card_query(idx[rows], types[rows], entities[rows])
    rows, low, high, selectable = rows[has], low[has], high[has], selectable[has]
    sizes = prep.n_hand[idx[rows]]
    for n in np.unique(sizes):
        pick = sizes == n
        subsets = Subsets.of(int(n))
        legal = subsets.legal(low[pick], high[pick], selectable[pick], prep.forced[idx[rows[pick]]])
        yield rows[pick], subsets, legal


def item_tokens(tokens: torch.Tensor, rows, position) -> torch.Tensor:
    """The token each of ``rows`` points at; zeros where ``position`` is negative."""
    position = torch.as_tensor(position)
    picked = tokens[torch.as_tensor(rows), position.clamp(min=0)]
    return picked * (position >= 0)[:, None]


@torch.no_grad()
def choose(model: Policy, prep: Prepared, idx: np.ndarray | None = None) -> list[FactoredAction]:
    """The most likely legal action for each example: the type first, then the item
    that type points at, then the cards."""
    idx = np.arange(prep.n) if idx is None else idx
    t = prep.tokens(idx)
    run, tokens = model.encode(t)
    types = model.type_logits(run, t["type_mask"]).argmax(-1).numpy()
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
        position[rows] = logits.argmax(-1).numpy()
        entities[rows] = position[rows] - prep.entity_base(idx[rows], types[rows])
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
        for row, best in zip(group, logits.argmax(-1).numpy()):
            cards[int(row)] = bits_to_cards(int(subsets.bits[best]))
    return [
        FactoredAction(
            action_type=int(types[row]),
            card_target=cards.get(row),
            entity_target=None if entities[row] < 0 else int(entities[row]),
        )
        for row in range(len(idx))
    ]
