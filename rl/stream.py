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
between moves once ``bot_log/model_stream/stop`` appears.
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
from jackdaw.env.action_space import FREE_ACTION_TYPES, ActionType, factored_to_engine_action
from jackdaw.env.balatro_env import _rust_view

from rl import dataset
from rl.convert import left_moves
from rl.evaluate import WIN_ANTE
from rl.features import MAX_FREE_MOVES, View, view_from
from rl.model import Prepared, choose
from rl.ordering import bot_order, order_can_change
from rl.train import load_model
from balatro_bot.files import replace_file  # noqa: E402
from fork_view import strip  # noqa: E402

STREAM = Path(__file__).resolve().parent.parent / "bot_log" / "model_stream"
# Moves the live game can't make are left out of what the model may pick.
LEGALITY = "balatrobot"
MAX_REFUSALS = 20
# Games in a row that end before their first move (an error, or a divergence at the
# start) before the stream gives up: the next game would only do the same.
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
                 stake: str = "WHITE", delay: float = 1.5, out: Path = STREAM,
                 seeds=None) -> None:
        self.adapter = LiveAdapter(backend, bundle_dir=out / "divergences", label="stream")
        self.model_path = Path(model_path)
        self.follow = follow
        self.deck, self.stake, self.delay = deck, stake, delay
        self.out = out
        self.seeds = iter(seeds) if seeds is not None else None
        self.model = None
        self.checkpoint = ""
        self.totals = {"games": 0, "wins": 0, "divergences": 0}
        self.recent: list[dict] = []
        self.now: dict = {}

    # -- files ------------------------------------------------------------------

    @property
    def stop_file(self) -> Path:
        return self.out / "stop"

    def stop_asked(self) -> bool:
        return self.stop_file.exists()

    def write_now(self, **changes) -> None:
        self.now.update(changes)
        self.out.mkdir(parents=True, exist_ok=True)
        body = {"pid": os.getpid(), "updated": time.time(), "model": str(self.model_path),
                "checkpoint": self.checkpoint, "game": self.now, **self.totals,
                "recent": self.recent}
        tmp = self.out / "now.tmp"
        try:
            tmp.write_text(json.dumps(body, indent=2))
            replace_file(tmp, self.out / "now.json")
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

    def pick(self, free: bool, refused: set) -> object:
        v = without(view(self.adapter.game, free), refused)
        if not v.mask.type_mask.any():
            return None
        action = choose(self.model, Prepared(dataset.examples_of([v])))[0]
        return action

    # -- one game ---------------------------------------------------------------

    def move(self, factored) -> None:
        """One chosen move; a play goes in the bot's order, its reorder as single swaps
        the live game shows."""
        game = self.adapter.game
        action = factored_to_engine_action(factored, game)
        if isinstance(action, A.PlayHand) and order_can_change(game, action.card_indices):
            order = bot_order(game, action.card_indices)
            if order != sorted(order):
                chosen = set(order)
                perm = order + [i for i in range(len(game.hand)) if i not in chosen]
                for at in left_moves(perm):
                    self.adapter.step(A.SwapHandLeft(at))
                action = A.PlayHand(tuple(range(len(order))))
        self.adapter.step(action)
        self.write_now(last=describe(action), **self.game_numbers())

    def game_numbers(self) -> dict:
        g = self.adapter.game
        return {"ante": int(g.ante), "round": int(g.round), "money": int(g.dollars),
                "chips": int(g.chips), "blind": int(g.blind.chips), "blind name": g.blind.key,
                "hands": int(g.hands_left), "discards": int(g.discards_left), "phase": str(g.phase)}

    def play_game(self) -> dict:
        self.load()
        seed = next(self.seeds) if self.seeds is not None else random_seed()
        self.now = {"seed": seed, "deck": self.deck, "stake": self.stake, "steps": 0, "last": ""}
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
                if self.delay:
                    time.sleep(self.delay)
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
        result["seconds"] = round(time.time() - started, 1)
        result["ended"] = time.time()
        return result

    def record(self, result: dict) -> None:
        if result["outcome"] != "stopped":
            self.totals["games"] += 1
            self.totals["wins"] += result["outcome"] == "won"
            self.totals["divergences"] += result["outcome"] == "divergence"
            self.out.mkdir(parents=True, exist_ok=True)
            with (self.out / "games.jsonl").open("a") as f:
                f.write(json.dumps(result) + "\n")
        self.recent = ([result] + self.recent)[:RECENT]
        self.write_now()

    def run(self, games: int | None = None) -> int:
        """Games until ``games``, a stop, or MAX_ERRORS games in a row that end before
        their first move; returns how many."""
        played = errors = 0
        while (games is None or played < games) and not self.stop_asked():
            result = self.play_game()
            self.record(result)
            played += 1
            dead = result["outcome"] == "error" or (result["outcome"] == "divergence" and not result["steps"])
            errors = errors + 1 if dead else 0
            if errors >= MAX_ERRORS:
                raise SystemExit(f"{errors} games in a row ended before their first move: {result['error']}")
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
