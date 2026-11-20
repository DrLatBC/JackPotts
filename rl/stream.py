"""Stream: a model plays the real game, move for move with a shadow Jackdaw run.

    .venv-rl\\Scripts\\python.exe -m rl.stream --port 12340 --model rl/data/ppo/r1/latest.pt --follow

``balatro_bot.stream`` starts the visible game and runs this as its child; the control
panel's Stream tab starts that. Each move goes through ``jackdaw.bridge.LiveAdapter``:
the shadow takes it first (a move the live game would refuse stops there), then the
live game, then the two views are compared. The model reads the shadow, the same
input it trained on. Plays go in the bot's left-to-right order, as single moves the
live game shows. A divergence ends that game (its repro bundle is kept) and the next
one starts.

Writes ``bot_log/model_stream/now.json`` (the game being played, totals, recent games) after
every move, appends each finished game to ``bot_log/model_stream/games.jsonl``, and stops
between moves once a stop is asked of the stream job (``balatro_bot.jobs``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sim" / "jackdaw_adapter"))

from jackdaw.bridge import DivergenceError, LiveAdapter, LiveBackend, RPCError
from jackdaw.core import IllegalActionError, NotPortedError, random_seed
from jackdaw.engine import actions as A
from jackdaw.engine.data import load as game_table
from jackdaw.env.action_space import FREE_ACTION_TYPES, ActionType, factored_to_engine_action
from jackdaw.env.balatro_env import _rust_view

from rl import dataset
from rl.evaluate import WIN_ANTE
from rl.features import CONSUMABLE, ENTITY_LIST, HAND, JOKER, MAX_FREE_MOVES, PACK, SHOP, View, view_from
from rl.model import Prepared, weigh
from rl.ordering import put_in_order
from rl.train import load_model
from balatro_bot import jobs  # noqa: E402
from fork_view import strip  # noqa: E402

# Moves the live game can't make are left out of what the model may pick.
LEGALITY = "balatrobot"
MAX_REFUSALS = 20
# Games in a row that fail (an error or a divergence) before their first play or
# discard before the stream gives up: the next game would only do the same. A blind
# pick doesn't count, since a live game that refuses every play still takes it.
MAX_ERRORS = 3
RECENT = 10


def view(game, free: bool) -> View:
    return view_from(game, *_rust_view(game._g.env_view(LEGALITY, free)))


def without(v: View, refused: set[tuple[int, int | None]]) -> View:
    """The view with the refused moves taken out of its mask. A move without an item
    takes its whole type out; one with an item takes out that item."""
    if not refused:
        return v
    mask = v.mask
    types = mask.type_mask.copy()
    entities = {t: m.copy() for t, m in mask.entity_masks.items()}
    for t, e in refused:
        if e is None or ActionType(t) not in entities:
            types[t] = False
            continue
        entities[ActionType(t)][e] = False
        if not entities[ActionType(t)].any():
            types[t] = False
    return replace(v, mask=replace(mask, type_mask=types, entity_masks=entities))


def describe(action) -> str:
    """A short line for the stream's last move: ``PlayHand 0 1 2``, ``CashOut``."""
    parts = [type(action).__name__]
    for value in vars(action).values():
        if isinstance(value, tuple):
            parts += [str(v) for v in value]
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts)


MOVES = {
    ActionType.PlayHand: "Play", ActionType.Discard: "Discard", ActionType.SelectBlind: "Play blind",
    ActionType.SkipBlind: "Skip blind", ActionType.CashOut: "Cash out", ActionType.Reroll: "Reroll",
    ActionType.NextRound: "Leave shop", ActionType.SkipPack: "Skip pack", ActionType.BuyCard: "Buy",
    ActionType.SellJoker: "Sell", ActionType.SellConsumable: "Sell", ActionType.UseConsumable: "Use",
    ActionType.RedeemVoucher: "Redeem", ActionType.OpenBooster: "Open", ActionType.PickPackCard: "Take",
    ActionType.SwapJokersLeft: "Move left", ActionType.SwapJokersRight: "Move right",
    ActionType.SwapHandLeft: "Move left", ActionType.SwapHandRight: "Move right",
    ActionType.SortHandRank: "Sort by rank", ActionType.SortHandSuit: "Sort by suit",
    ActionType.RerollBoss: "Reroll boss", ActionType.BuyAndUse: "Buy and use",
}
SUITS = {"Spades": "♠", "Hearts": "♥", "Clubs": "♣", "Diamonds": "♦"}
RANKS = {"Jack": "J", "Queen": "Q", "King": "K", "Ace": "A"}
# Where each item list sits on the game; the shop's list is cards, vouchers, boosters.
ITEM_AREAS = {HAND: ("hand",), JOKER: ("jokers",), CONSUMABLE: ("consumables",),
              SHOP: ("shop_jokers", "shop_vouchers", "shop_boosters"), PACK: ("pack_cards",)}


def title(key: str) -> str:
    """The game's name for a center or blind key: ``Gros Michel``, ``The Hook``."""
    table = game_table("blinds") if key.startswith("bl_") else game_table("centers")
    return table.get(key, {}).get("name", key)


def card_name(card) -> dict:
    """A card as the overlay draws it: a playing card's rank and suit (``?`` face down),
    else the item's name; ``debuff`` when the boss has disabled it."""
    if card.front is None:
        return {"name": title(card.key)}
    if card.face_down:
        return {"rank": "?"}
    if card.key == "m_stone":
        out = {"rank": "Stone"}
    else:
        out = {"rank": RANKS.get(card.rank, card.rank), "suit": SUITS[card.suit]}
    if card.debuff:
        out["debuff"] = True
    return out


def option(factored, game) -> dict:
    """A move the model weighed, named for the overlay: ``{"move": "Play", "hand":
    "Two Pair", "cards": [...]}``, ``{"move": "Buy", "item": {...}}``."""
    t = ActionType(factored.action_type)
    out = {"move": MOVES[t]}
    if factored.entity_target is not None and int(t) in ENTITY_LIST:
        items, part = ENTITY_LIST[int(t)]
        area = getattr(game, ITEM_AREAS[items][part])
        if factored.entity_target < len(area):
            out["item"] = card_name(area[factored.entity_target])
    if factored.card_target and t not in (ActionType.SwapHandLeft, ActionType.SwapHandRight):
        cards = [game.hand[i] for i in factored.card_target]
        out["cards"] = [card_name(c) for c in cards]
        if t == ActionType.PlayHand and not any(c.face_down for c in cards):
            scored = game.scoring_cards(factored.card_target)
            if scored is not None:
                out["hand"] = scored[0]
    return out


def deck_view(game) -> dict:
    """The whole deck for the overlay: per card (``"SA"``, ``"H10"``) how many the deck
    holds, how many are still to draw, in hand (the rest were played or discarded this
    round) and debuffed, and its enhanced cards counted by name. Stone cards have no
    rank or suit, so they are only counted."""
    cards: dict[str, dict] = {}
    enhanced: dict[str, int] = {}
    piles = (("draw", game.deck), ("hand", game.hand), (None, game.discard_pile))
    size = 0
    for where, pile in piles:
        for card in pile:
            if card.front is None:
                continue
            size += 1
            if card.key != "c_base":
                name = title(card.key).removesuffix(" Card")
                enhanced[name] = enhanced.get(name, 0) + 1
            if card.key == "m_stone":
                continue
            cell = cards.setdefault(card.front[0] + RANKS.get(card.rank, card.rank), {"n": 0, "draw": 0, "hand": 0, "debuff": 0})
            cell["n"] += 1
            cell["debuff"] += bool(card.debuff)
            if where:
                cell[where] += 1
    return {"size": size, "to draw": len(game.deck), "cards": cards,
            "enhanced": dict(sorted(enhanced.items(), key=lambda kv: -kv[1]))}


class UpstreamView:
    """The live game with the JackPotts fork's extra gamestate fields taken out of every
    answer. The shadow speaks upstream balatrobot, so without this every card differs
    (the fork's ``value.ability``) before the first move."""

    def __init__(self, backend) -> None:
        self.backend = backend

    def handle(self, method: str, params=None):
        return strip(self.backend.handle(method, params))

    def __getattr__(self, name):
        return getattr(self.backend, name)


class Player:
    def __init__(self, backend, model_path: Path, *, follow: bool = False, deck: str = "BLUE",
                 stake: str = "WHITE", delay: float = 1.5, out: Path | None = None,
                 seeds=None) -> None:
        self.out = Path(out) if out is not None else jobs.stream().folder
        self.job = jobs.Job(self.out, "stream")
        self.adapter = LiveAdapter(backend, bundle_dir=self.out / "divergences", label="stream")
        self.model_path = Path(model_path)
        self.follow = follow
        self.deck, self.stake, self.delay = deck, stake, delay
        self.seeds = iter(seeds) if seeds is not None else None
        self.model = None
        self.checkpoint = ""
        self.updates: int | None = None
        self.first_updates: int | None = None
        self.totals = {"games": 0, "wins": 0, "divergences": 0, "hands": 0}
        self.antes: list[int] = []
        self.recent: list[dict] = []
        self.now: dict = {}

    # -- files ------------------------------------------------------------------

    def stop_asked(self) -> bool:
        return self.job.asked("stop")

    def write_now(self, **changes) -> None:
        self.now.update(changes)
        self.out.mkdir(parents=True, exist_ok=True)
        body = {"pid": os.getpid(), "updated": time.time(), "model": str(self.model_path),
                "checkpoint": self.checkpoint, "updates": self.updates,
                "first updates": self.first_updates, "game": self.now, **self.totals,
                "mean ante": round(sum(self.antes) / len(self.antes), 2) if self.antes else None,
                "recent": self.recent}
        try:
            jobs.write_json(self.out / "now.json", body)
        except OSError:
            pass  # what the panel shows; the next move writes it again

    # -- the model --------------------------------------------------------------

    def load(self) -> None:
        """The model, read again before each game when following a run's latest save."""
        if self.model is not None and not self.follow:
            return
        try:
            model, meta = load_model(self.model_path)
        except (OSError, RuntimeError, EOFError):
            if self.model is None:
                raise
            return  # caught mid-save by the run; the model already loaded plays on
        model.eval()
        self.model = model
        updates = meta.get("updates")
        self.checkpoint = self.model_path.stem + (f" (update {updates})" if updates is not None else "")
        self.updates = updates
        if self.first_updates is None:
            self.first_updates = updates

    def pick(self, free: bool, refused: set) -> object:
        """The model's move, with what it weighed written for the overlay before the
        move is made."""
        v = without(view(self.adapter.game, free), refused)
        if not v.mask.type_mask.any():
            return None
        w = weigh(self.model, Prepared(dataset.examples_of([v])))
        game = self.adapter.game
        self.write_now(outlook=round(w.value, 2), considering=[
            {**option(a, game), "p": round(w.types[a.action_type], 3), "sure": round(p, 3)}
            for a, p in w.options
        ])
        return w.options[0][0]

    # -- one game ---------------------------------------------------------------

    def move(self, factored) -> None:
        """One chosen move; a play goes in the bot's order, its reorder as single swaps
        the live game shows."""
        game = self.adapter.game
        action = factored_to_engine_action(factored, game)
        if isinstance(action, A.PlayHand):
            action = A.PlayHand(put_in_order(game, action.card_indices, step=self.adapter.step))
        self.adapter.step(action)
        self.write_now(last=describe(action), **self.game_numbers())

    def game_numbers(self) -> dict:
        g = self.adapter.game
        return {"ante": int(g.ante), "round": int(g.round), "money": int(g.dollars),
                "chips": int(g.chips), "blind": int(g.blind.chips), "blind name": g.blind.key,
                "blind title": title(g.blind.key),
                "hands": int(g.hands_left), "discards": int(g.discards_left), "phase": str(g.phase),
                "deck": deck_view(g)}

    def play_game(self) -> dict:
        self.load()
        seed = next(self.seeds) if self.seeds is not None else random_seed()
        self.now = {"seed": seed, "deck": self.deck, "stake": self.stake, "steps": 0, "last": "",
                    "hands played": 0, "discards": 0, "history": []}
        result = {"seed": seed, "deck": self.deck, "stake": self.stake, "checkpoint": self.checkpoint}
        started = time.time()
        free_run, streak, refused = 0, 0, set()
        try:
            self.adapter.start(self.deck, self.stake, seed)
            self.write_now(**self.game_numbers())
            while True:
                if self.adapter.won:
                    result["outcome"] = "won"
                    break
                if self.adapter.done:
                    result["outcome"] = "lost"
                    break
                if self.stop_asked():
                    result["outcome"] = "stopped"
                    break
                factored = self.pick(free_run < MAX_FREE_MOVES, refused)
                if factored is None:
                    result["outcome"] = "stuck"
                    break
                if self.delay:  # the overlay shows what was weighed before the move lands
                    time.sleep(self.delay)
                try:
                    self.move(factored)
                except IllegalActionError as e:  # the shadow refused it before the live game saw it
                    streak += 1
                    refused.add((factored.action_type, factored.entity_target))
                    if streak > MAX_REFUSALS:
                        result["outcome"], result["error"] = "stuck", str(e)
                        break
                    continue
                streak, refused = 0, set()
                free_run = free_run + 1 if factored.action_type in FREE_ACTION_TYPES else 0
                self.now["steps"] += 1
                if factored.action_type == ActionType.PlayHand:
                    self.now["hands played"] += 1
                    self.totals["hands"] += 1
                elif factored.action_type == ActionType.Discard:
                    self.now["discards"] += 1
                self.now["history"].append(self.now["outlook"])
        except DivergenceError as e:
            result["outcome"] = "divergence"
            result["error"] = str(e).splitlines()[0]
            result["bundle"] = None if e.bundle_path is None else str(e.bundle_path)
        except NotPortedError as e:
            result["outcome"], result["error"] = "not ported", str(e)
        except (RPCError, OSError) as e:  # the live game refused or went away
            result["outcome"], result["error"] = "error", f"{type(e).__name__}: {e}"
        if self.adapter.started:
            g = self.adapter.game
            result["ante"] = WIN_ANTE if result["outcome"] == "won" else min(int(g.ante), WIN_ANTE)
            result["round"] = int(g.round)
        result["steps"] = self.now["steps"]
        result["hands"] = self.now["hands played"]
        result["discards"] = self.now["discards"]
        result["seconds"] = round(time.time() - started, 1)
        result["ended"] = time.time()
        return result

    def record(self, result: dict) -> None:
        if result["outcome"] != "stopped":
            self.totals["games"] += 1
            self.totals["wins"] += result["outcome"] == "won"
            self.totals["divergences"] += result["outcome"] == "divergence"
            if "ante" in result:
                self.antes.append(result["ante"])
            self.out.mkdir(parents=True, exist_ok=True)
            with (self.out / "games.jsonl").open("a") as f:
                f.write(json.dumps(result) + "\n")
        self.recent = ([result] + self.recent)[:RECENT]
        self.write_now()

    def run(self, games: int | None = None) -> int:
        """Games until ``games``, a stop, or MAX_ERRORS games in a row that fail before
        their first play or discard; returns how many."""
        played = errors = 0
        while (games is None or played < games) and not self.stop_asked():
            result = self.play_game()
            self.record(result)
            played += 1
            dead = (result["outcome"] in ("error", "divergence")
                    and not result.get("hands") and not result.get("discards"))
            errors = errors + 1 if dead else 0
            if errors >= MAX_ERRORS:
                raise SystemExit(f"{errors} games in a row failed before their first play or discard: {result['error']}")
        return played


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=12340)
    ap.add_argument("--model", required=True, help="a .pt: a run's latest.pt or u#####.pt, or a copy")
    ap.add_argument("--follow", action="store_true", help="read the model again before each game")
    ap.add_argument("--deck", default="BLUE")
    ap.add_argument("--stake", default="WHITE")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between moves")
    ap.add_argument("--games", type=int, default=None)
    args = ap.parse_args()
    torch.set_num_threads(1)
    player = Player(UpstreamView(LiveBackend(port=args.port, timeout=120.0)), Path(args.model), follow=args.follow,
                    deck=args.deck, stake=args.stake, delay=args.delay)
    player.run(args.games)
    return 0


if __name__ == "__main__":
    sys.exit(main())
