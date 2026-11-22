"""Training examples on disk: one ``.npz`` shard per few hundred games.

A shard is a cache built from replays by ``rl.convert`` and can always be rebuilt;
it is stamped with the Jackdaw version and action-space version it was made on.
One example is one bot decision: the view before it (``rl.features.View``) and the
action the bot took. Lists that vary in length (hand, jokers, shop...) are stored
end to end with a count per example.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from jackdaw.env.action_space import ACTION_VERSION, NUM_ACTION_TYPES
from jackdaw.env.game_spec import FactoredAction
from jackdaw.env.observation import D_CONSUMABLE, D_GLOBAL, D_JOKER, D_PLAYING_CARD, D_SHOP

from rl.features import D_CONS_EXTRA, D_PACK_EXTRA, D_ROUND, ENTITY_SLOT, ENTITY_TYPES, View

FORMAT = 1

# Example flags.
FOLDED = 1
"""A play the bot made after reordering its hand, stored as one play of the cards it
chose, seen from before the reorder."""
ORDER_MATTERS = 2
"""A folded play that scores differently in hand order than in the bot's order."""
BELL = 4
"""Cerulean Bell forces a card into the selection."""
CAPPED = 8
"""Reorder moves beyond the environment's cap come before this action, unseen."""

# Reorder moves the examples leave out, replayed on the game before the action: a
# joker or hand card moving one place left.
PRE_JOKER_LEFT = 0
PRE_HAND_LEFT = 1000

RAGGED: dict[str, tuple[int, type]] = {
    "hand": (D_PLAYING_CARD, np.float32),
    "joker": (D_JOKER, np.float32),
    "cons": (D_CONSUMABLE, np.float32),
    "shop": (D_SHOP, np.float32),
    "pack": (D_PLAYING_CARD, np.float32),
    "cons_extra": (D_CONS_EXTRA, np.float32),
    "pack_extra": (D_PACK_EXTRA, np.float32),
    "card_mask": (0, np.bool_),
    "emask": (0, np.bool_),
    "pre": (0, np.int16),
}


def cards_to_bits(cards) -> int:
    bits = 0
    for c in cards or ():
        bits |= 1 << int(c)
    return bits


def bits_to_cards(bits: int) -> tuple[int, ...]:
    return tuple(i for i in range(int(bits).bit_length()) if (int(bits) >> i) & 1)


class ShardWriter:
    """Collects examples game by game and writes them as one shard."""

    def __init__(self) -> None:
        self._fixed: dict[str, list] = {k: [] for k in (
            "glob", "round", "splits", "type_mask", "min_sel", "max_sel", "forced", "act_type",
            "act_entity", "act_cards", "exec_cards", "flags", "game", "emask_len",
        )}
        self._rag: dict[str, list] = {k: [] for k in RAGGED}
        self._rag_n: dict[str, list] = {k: [] for k in RAGGED}
        self._games: dict[str, list] = {k: [] for k in (
            "seed", "won", "final", "n_examples", "source", "deck", "stake",
        )}

    @property
    def n_games(self) -> int:
        return len(self._games["seed"])

    def start_game(self, seed: str, source: str, deck: str, stake: int) -> None:
        """Begin a game: its seed, the replay file it came from, and the deck key
        (``"b_blue"``) and stake number it was played on."""
        self._games["seed"].append(seed)
        self._games["source"].append(source)
        self._games["deck"].append(deck)
        self._games["stake"].append(stake)
        self._games["n_examples"].append(0)

    def end_game(self, won: bool, final: dict) -> None:
        self._games["won"].append(bool(won))
        self._games["final"].append((final["ante"], final["round"], final["money"]))

    def add(
        self,
        view: View,
        action: FactoredAction,
        *,
        exec_cards: tuple[int, ...] | None = None,
        pre: list[int] | None = None,
        flags: int = 0,
    ) -> None:
        obs, mask = view.obs, view.mask
        f = self._fixed
        f["glob"].append(obs.global_context.copy())
        f["round"].append(view.round_extra.copy())
        f["splits"].append(view.splits)
        f["type_mask"].append(mask.type_mask.copy())
        f["min_sel"].append(mask.min_card_select)
        f["max_sel"].append(mask.max_card_select)
        f["forced"].append(-1 if mask.forced_card is None else mask.forced_card)
        f["act_type"].append(int(action.action_type))
        f["act_entity"].append(-1 if action.entity_target is None else action.entity_target)
        f["act_cards"].append(cards_to_bits(action.card_target))
        f["exec_cards"].append(
            cards_to_bits(action.card_target if exec_cards is None else exec_cards)
        )
        f["flags"].append(flags)
        f["game"].append(self.n_games - 1)
        lens = np.zeros(len(ENTITY_TYPES), np.int16)
        parts = []
        for slot, t in enumerate(ENTITY_TYPES):
            m = mask.entity_masks.get(t)
            if m is not None:
                lens[slot] = len(m)
                parts.append(m)
        f["emask_len"].append(lens)
        rows = {
            "hand": obs.hand_cards, "joker": obs.jokers, "cons": obs.consumables,
            "shop": obs.shop_cards, "pack": obs.pack_cards, "cons_extra": view.cons_extra,
            "pack_extra": view.pack_extra, "card_mask": mask.card_mask,
            "emask": np.concatenate(parts) if parts else np.zeros(0, np.bool_),
            "pre": np.asarray(pre or [], np.int16),
        }
        for name, arr in rows.items():
            self._rag[name].append(np.array(arr, copy=True))
            self._rag_n[name].append(len(arr))
        self._games["n_examples"][-1] += 1

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, **self.arrays())
        tmp.replace(path)

    def arrays(self) -> dict[str, np.ndarray]:
        """Everything collected so far, as a shard's arrays."""
        out: dict[str, np.ndarray] = {
            "format": np.int64(FORMAT),
            "jackdaw": np.str_(importlib.metadata.version("jackdaw")),
            "action_version": np.int64(ACTION_VERSION),
        }
        f = self._fixed
        n = len(f["act_type"])
        out["glob"] = np.asarray(f["glob"], np.float32).reshape(n, D_GLOBAL)
        out["round"] = np.asarray(f["round"], np.float32).reshape(n, D_ROUND)
        out["splits"] = np.asarray(f["splits"], np.int16).reshape(n, 3)
        out["type_mask"] = np.asarray(f["type_mask"], np.bool_).reshape(n, NUM_ACTION_TYPES)
        for name, dtype in (("min_sel", np.int8), ("max_sel", np.int8), ("forced", np.int8),
                            ("act_type", np.int8), ("act_entity", np.int16),
                            ("act_cards", np.int64), ("exec_cards", np.int64),
                            ("flags", np.uint8), ("game", np.int32)):
            out[name] = np.asarray(f[name], dtype)
        out["emask_len"] = np.asarray(f["emask_len"], np.int16).reshape(n, len(ENTITY_TYPES))
        for name, (width, dtype) in RAGGED.items():
            shape = (-1, width) if width else (-1,)
            parts = self._rag[name]
            out[name] = (np.concatenate(parts).astype(dtype, copy=False).reshape(shape)
                         if parts else np.zeros((0, width) if width else (0,), dtype))
            out[name + "_n"] = np.asarray(self._rag_n[name], np.int32)
        g = self._games
        out["g_seed"] = np.asarray(g["seed"], np.str_)
        out["g_source"] = np.asarray(g["source"], np.str_)
        out["g_deck"] = np.asarray(g["deck"], np.str_)
        out["g_stake"] = np.asarray(g["stake"], np.int8)
        out["g_won"] = np.asarray(g["won"], np.bool_)
        out["g_final"] = np.asarray(g["final"], np.int32).reshape(len(g["seed"]), 3)
        out["g_n"] = np.asarray(g["n_examples"], np.int32)
        return out


def examples_of(views: list[View]) -> Examples:
    """Views with no action yet (a model is about to choose one), as examples."""
    writer = ShardWriter()
    writer.start_game("", "", "", 0)
    for view in views:
        writer.add(view, FactoredAction(action_type=0))
    writer.end_game(False, {"ante": 0, "round": 0, "money": 0})
    return Examples(writer.arrays())


@dataclass
class Examples:
    """Examples in memory: the arrays of one shard, or of several joined."""

    arrays: dict[str, np.ndarray]

    def __post_init__(self) -> None:
        if "round" not in self.arrays:  # shards without the round columns read as zeros
            self.arrays["round"] = np.zeros((len(self.arrays["act_type"]), D_ROUND), np.float32)
        self.start: dict[str, np.ndarray] = {}
        for name in RAGGED:
            n = self.arrays[name + "_n"].astype(np.int64)
            self.start[name] = np.concatenate([[0], np.cumsum(n)])
        self.g_start = np.concatenate([[0], np.cumsum(self.arrays["g_n"].astype(np.int64))])

    def __len__(self) -> int:
        return len(self.arrays["act_type"])

    def __getitem__(self, name: str) -> np.ndarray:
        return self.arrays[name]

    @property
    def n_games(self) -> int:
        return len(self.arrays["g_seed"])

    def rows(self, name: str, i: int) -> np.ndarray:
        s = self.start[name]
        return self.arrays[name][s[i]:s[i + 1]]

    def entity_mask(self, i: int, action_type: int) -> np.ndarray:
        """The mask over the items the action type points at, for example ``i``."""
        lens = self.arrays["emask_len"][i]
        slot = ENTITY_SLOT[int(action_type)]
        at = self.start["emask"][i] + int(lens[:slot].sum())
        return self.arrays["emask"][at:at + int(lens[slot])]

    def game_examples(self, g: int) -> range:
        return range(int(self.g_start[g]), int(self.g_start[g + 1]))

    def action(self, i: int) -> FactoredAction:
        """The bot's action for example ``i``, as the model is asked to predict it."""
        return self._action(i, "act_cards")

    def exec_action(self, i: int) -> FactoredAction:
        """The action that replays example ``i`` on a game the example's ``pre`` moves
        were applied to."""
        return self._action(i, "exec_cards")

    def _action(self, i: int, cards: str) -> FactoredAction:
        a = self.arrays
        bits = int(a[cards][i])
        entity = int(a["act_entity"][i])
        return FactoredAction(
            action_type=int(a["act_type"][i]),
            card_target=bits_to_cards(bits) if bits else None,
            entity_target=None if entity < 0 else entity,
        )


def load_shard(path: str | Path) -> Examples:
    with np.load(path) as z:
        arrays = {k: z[k] for k in z.files}
    if int(arrays["format"]) != FORMAT:
        raise ValueError(f"{path}: shard format {arrays['format']}, expected {FORMAT}")
    return Examples(arrays)


def load_shards(paths) -> Examples:
    """Several shards joined into one set of examples, games renumbered in order."""
    shards = [load_shard(p).arrays for p in paths]
    if not shards:
        raise ValueError("no shards to load")
    stamps = {(str(s["jackdaw"]), int(s["action_version"])) for s in shards}
    if len(stamps) != 1:
        raise ValueError(f"shards were made on different simulator versions: {sorted(stamps)}")
    out: dict[str, np.ndarray] = {k: shards[0][k] for k in ("format", "jackdaw", "action_version")}
    offset = 0
    games = []
    for s in shards:
        games.append(s["game"] + offset)
        offset += len(s["g_seed"])
    for key in shards[0]:
        if key in out:
            continue
        out[key] = np.concatenate(games if key == "game" else [s[key] for s in shards])
    return Examples(out)
